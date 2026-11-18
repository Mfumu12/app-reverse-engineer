#!/usr/bin/env python3
"""Offline APK recon: manifest, permissions, components, SDKs, endpoints, certs, secrets.

Usage:
  python3 tools/apk_recon.py app.apk [--json out.json]

Designed to be dependency-light: androguard is used when importable (manifest /
resources / certificates), everything else falls back to plain zipfile + regex so
it still produces useful evidence without any third-party package.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- patterns

URL_RE = re.compile(rb"(?:https?|wss?|rtmp|rtmps|ftp)://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{4,200}")
DOMAIN_RE = re.compile(
    rb"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    rb"(?:com|net|org|io|tv|app|xyz|co|me|info|cloud|dev|site|online|link|pro|biz|br|ao|pt|m3u8|top|live|stream|club|vip)\b",
    re.I,
)
IP_RE = re.compile(rb"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_HINTS = [
    b"AIza", b"AKIA", b"sk_live", b"sk_test", b"pk_live", b"ghp_", b"xoxb-",
    b"-----BEGIN", b"client_secret", b"api_key", b"apiKey", b"apikey",
    b"access_token", b"auth_token", b"secret_key", b"password", b"Bearer ",
]

# package prefix -> (vendor, category)
SDK_SIGNATURES = {
    "com/google/firebase": ("Firebase", "backend/analytics"),
    "com/google/android/gms": ("Google Play Services", "platform"),
    "com/google/android/exoplayer": ("ExoPlayer / Media3", "player"),
    "androidx/media3": ("Media3 (ExoPlayer)", "player"),
    "com/google/android/material": ("Material Components", "ui"),
    "com/google/gson": ("Gson", "json"),
    "com/squareup/okhttp": ("OkHttp", "networking"),
    "com/squareup/retrofit": ("Retrofit", "networking"),
    "com/squareup/picasso": ("Picasso", "images"),
    "com/bumptech/glide": ("Glide", "images"),
    "io/coil-kt": ("Coil", "images"),
    "com/facebook": ("Facebook SDK", "ads/analytics"),
    "com/applovin": ("AppLovin", "ads"),
    "com/unity3d": ("Unity Ads/Engine", "ads/engine"),
    "com/ironsource": ("ironSource", "ads"),
    "com/google/ads": ("Google Mobile Ads", "ads"),
    "com/onesignal": ("OneSignal", "push"),
    "com/amplitude": ("Amplitude", "analytics"),
    "com/mixpanel": ("Mixpanel", "analytics"),
    "io/sentry": ("Sentry", "crash"),
    "com/crashlytics": ("Crashlytics", "crash"),
    "com/bugsnag": ("Bugsnag", "crash"),
    "com/stripe": ("Stripe", "payments"),
    "com/android/billingclient": ("Google Play Billing", "payments"),
    "com/google/android/play/core": ("Play Core (in-app update)", "platform"),
    "com/android/installreferrer": ("Install Referrer", "analytics"),
    "io/flutter": ("Flutter", "framework"),
    "com/facebook/react": ("React Native", "framework"),
    "org/chromium": ("Chromium/WebView", "platform"),
    "com/cleveroad": ("Cleveroad libs", "ui"),
    "com/github/barteksc": ("Pdfium (PDF viewer)", "ui"),
    "com/airbnb/lottie": ("Lottie", "ui"),
    "org/videolan": ("LibVLC", "player"),
    "com/arthenica": ("FFmpeg-Kit", "media"),
    "tv/danmaku/ijk": ("ijkplayer", "player"),
    "com/dexterous": ("Dexter (permissions)", "ui"),
    "com/koushikdutta": ("Ion/AsyncHttp", "networking"),
    "com/loopj/android": ("AsyncHttpClient", "networking"),
    "com/android/volley": ("Volley", "networking"),
    "com/google/firebase/crashlytics": ("Crashlytics", "crash"),
    "com/huawei": ("Huawei HMS", "platform"),
    "com/appsflyer": ("AppsFlyer", "analytics"),
    "com/adjust": ("Adjust", "analytics"),
    "com/braze": ("Braze", "messaging"),
    "androidx/work": ("WorkManager", "platform"),
    "androidx/room": ("Room", "storage"),
    "io/realm": ("Realm", "storage"),
    "com/raizlabs": ("DBFlow", "storage"),
}

DANGEROUS_PERMS = {
    "READ_SMS", "RECEIVE_SMS", "SEND_SMS", "READ_CALL_LOG", "WRITE_CALL_LOG",
    "CALL_PHONE", "READ_CONTACTS", "WRITE_CONTACTS", "READ_PHONE_STATE",
    "READ_PHONE_NUMBERS", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
    "ACCESS_BACKGROUND_LOCATION", "RECORD_AUDIO", "CAMERA", "READ_EXTERNAL_STORAGE",
    "WRITE_EXTERNAL_STORAGE", "MANAGE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES",
    "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO", "SYSTEM_ALERT_WINDOW",
    "REQUEST_INSTALL_PACKAGES", "QUERY_ALL_PACKAGES", "BIND_ACCESSIBILITY_SERVICE",
    "PACKAGE_USAGE_STATS", "POST_NOTIFICATIONS", "FOREGROUND_SERVICE",
    "FOREGROUND_SERVICE_MEDIA_PLAYBACK", "FOREGROUND_SERVICE_DATA_SYNC",
}


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def _a(el, name):
    """Read an android: namespaced attribute from a manifest element."""
    return el.get(ANDROID_NS + name) or el.get("android:" + name)


def _printable_strings(data: bytes, minlen: int = 5):
    return re.findall(rb"[\x20-\x7e]{%d,400}" % minlen, data)


def entropy_ish(s: bytes) -> float:
    if not s:
        return 0.0
    counts = collections.Counter(s)
    n = len(s)
    import math
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ---------------------------------------------------------------- main recon
def recon(path: Path) -> dict:
    out: dict = {
        "file": {"path": str(path), "size": path.stat().st_size},
        "hashes": {},
        "zip": {},
        "manifest": {},
        "components": {},
        "native": {},
        "sdk_hits": {},
        "endpoints": {},
        "secrets": [],
        "certificates": [],
        "strings_of_interest": [],
        "assets": [],
        "apk_splits_or_payload": [],
    }
    blob = path.read_bytes()
    out["file"]["sha256"] = hashlib.sha256(blob).hexdigest()
    out["file"]["md5"] = hashlib.md5(blob).hexdigest()

    if not zipfile.is_zipfile(path):
        out["error"] = "not a zip/apk"
        return out

    zf = zipfile.ZipFile(path)
    infos = zf.infolist()
    by_ext: dict[str, list[int]] = collections.defaultdict(list)
    for i in infos:
        ext = Path(i.filename).suffix.lower() or "<none>"
        by_ext[ext].append(i.file_size)

    out["zip"]["entries"] = len(infos)
    out["zip"]["uncompressed"] = sum(i.file_size for i in infos)
    out["zip"]["by_extension"] = {
        ext: {"count": len(v), "bytes": sum(v)} for ext, v in sorted(by_ext.items(), key=lambda kv: -sum(kv[1]))
    }
    out["zip"]["biggest"] = [
        {"name": i.filename, "size": i.file_size}
        for i in sorted(infos, key=lambda i: -i.file_size)[:15]
    ]
    out["zip"]["has_v1_signature"] = any(
        n.startswith("META-INF/") and n.upper().endswith((".RSA", ".DSA", ".EC")) for n in zf.namelist()
    )
    names = zf.namelist()
    out["zip"]["dex_files"] = [n for n in names if re.fullmatch(r"classes\d*\.dex", n)]
    out["zip"]["has_no_extract_native"] = any(n.endswith(".so") for n in names)

    # ---- native libs
    for n in names:
        if n.startswith("lib/") and n.endswith(".so"):
            arch = n.split("/")[1]
            out["native"].setdefault(arch, []).append({"name": n.rsplit("/", 1)[-1], "size": zf.getinfo(n).file_size})
    for arch in out["native"]:
        out["native"][arch].sort(key=lambda x: -x["size"])

    # ---- flutter / react native / cordova markers
    out["zip"]["frameworks"] = [
        m for m in ("libflutter.so", "libapp.so", "libreactnativejni.so", "index.android.bundle",
                    "assets/www/cordova.js", "lib/arm64-v8a/libhermes.so", "assets/index.android.bundle")
        if any(n == m or n.endswith(m) for n in names)
    ]

    # ---- assets of interest
    for n in names:
        if n.startswith("assets/") or n.startswith("res/raw/"):
            ext = Path(n).suffix.lower()
            if ext in {".json", ".xml", ".txt", ".properties", ".yml", ".yaml", ".js", ".html", ".m3u", ".m3u8", ".cfg", ".conf"} and zf.getinfo(n).file_size < 3_000_000:
                out["assets"].append({"name": n, "size": zf.getinfo(n).file_size})

    # ---- scan bytes for endpoints / secrets
    urls: dict[str, int] = collections.Counter()
    domains: dict[str, int] = collections.Counter()
    ips: dict[str, int] = collections.Counter()
    emails: dict[str, int] = collections.Counter()
    secret_hits: list[dict] = []

    scan_targets = [n for n in names if re.fullmatch(r"classes\d*\.dex", n)]
    scan_targets += [n for n in names if n == "resources.arsc"] or []
    scan_targets += [a["name"] for a in out["assets"]]

    for n in scan_targets:
        try:
            data = zf.read(n)
        except Exception:
            continue
        for m in URL_RE.findall(data):
            u = m.decode("utf-8", "ignore").rstrip(".,;)\"'")
            if re.search(r"(schemas\.android\.com|w3\.org|apache\.org|json-schema\.org|github\.com/|xmlns)", u):
                continue
            urls[u] += 1
        for m in DOMAIN_RE.findall(data):
            d = m.decode("ascii", "ignore").lower()
            if not re.match(r"^\d", d) and d.count(".") >= 1 and len(d) > 5:
                domains[d] += 1
        for m in IP_RE.findall(data):
            ip = m.decode()
            if not ip.startswith(("0.", "127.", "255.")):
                ips[ip] += 1
        for m in EMAIL_RE.findall(data):
            emails[m.decode("utf-8", "ignore")] += 1
        for hint in SECRET_HINTS:
            for idx in [i for i in range(len(data)) if data.startswith(hint, i)][:12]:
                chunk = data[idx: idx + 120]
                txt = "".join(chr(c) if 32 <= c < 127 else "·" for c in chunk)
                if entropy_ish(chunk) < 3.2:
                    continue
                secret_hits.append({"file": n, "hint": hint.decode(), "context": txt})

    out["endpoints"]["urls"] = [{"url": u, "hits": c} for u, c in urls.most_common(120)]
    out["endpoints"]["domains"] = [{"domain": d, "hits": c} for d, c in domains.most_common(120)]
    out["endpoints"]["ips"] = [{"ip": i, "hits": c} for i, c in ips.most_common(40)]
    out["endpoints"]["emails"] = [{"email": e, "hits": c} for e, c in emails.most_common(20)]
    out["secrets"] = secret_hits[:80]

    # ---- SDK signature scan (dex type descriptors)
    dex_blob = b"".join(zf.read(n) for n in out["zip"]["dex_files"])
    sdk_hits: dict[str, dict] = {}
    for prefix, (vendor, category) in SDK_SIGNATURES.items():
        needle = ("L" + prefix).encode()
        count = dex_blob.count(needle)
        if count:
            sdk_hits[vendor] = {"category": category, "refs": count}
    out["sdk_hits"] = dict(sorted(sdk_hits.items(), key=lambda kv: -kv[1]["refs"]))

    # ---- androguard: manifest, certs, resources
    try:
        from androguard.core.apk import APK  # type: ignore

        a = APK(str(path))
        m = out["manifest"]
        m.update({
            "package": a.get_package(),
            "app_name": a.get_app_name(),
            "version_name": a.get_androidversion_name(),
            "version_code": a.get_androidversion_code(),
            "min_sdk": a.get_min_sdk_version(),
            "target_sdk": a.get_target_sdk_version(),
            "effective_target_sdk": a.get_effective_target_sdk_version(),
            "permissions": sorted(a.get_permissions()),
            "declared_permissions": sorted(a.get_declared_permissions()),
            "features": sorted(a.get_features()),
            "main_activity": a.get_main_activity(),
            "app_name_resolved": a.get_app_name(),
            "libraries": sorted(a.get_libraries()),
        })
        m["dangerous_permissions"] = [
            p for p in m["permissions"] if p.rsplit(".", 1)[-1] in DANGEROUS_PERMS
        ]
        app_attrs = {}
        for attr in ("usesCleartextTraffic", "networkSecurityConfig", "allowBackup",
                     "debuggable", "extractNativeLibs", "usesNonSdkApi", "largeHeap",
                     "requestLegacyExternalStorage", "testOnly", "label", "icon"):
            v = a.get_attribute_value("application", attr)
            if v is not None:
                app_attrs[attr] = v
        m["application_attributes"] = app_attrs

        comps = out["components"]
        exported = a.get_all_attribute_value("activity", "exported")
        comps["activities"] = sorted(a.get_activities())
        comps["services"] = sorted(a.get_services())
        comps["receivers"] = sorted(a.get_receivers())
        comps["providers"] = sorted(a.get_providers())
        # deep links + exported components + custom schemes
        links, exp_acts, exp_svc, exp_recv, schemes = [], [], [], [], set()
        try:
            for el in a.get_android_manifest_xml().iter():
                tag = el.tag.split("}")[-1]
                if tag in ("activity", "activity-alias", "service", "receiver", "provider"):
                    # a component is effectively exported when android:exported="true",
                    # or when it has an intent-filter and no explicit exported="false"
                    declared = _a(el, "exported")
                    has_intent = any(c.tag.split("}")[-1] == "intent-filter" for c in el)
                    effective = (str(declared).lower() == "true") or (declared is None and has_intent)
                    if effective:
                        item = {"name": _a(el, "name"), "declared": declared, "has_intent_filter": has_intent}
                        if tag.startswith("activity"):
                            exp_acts.append(item)
                        elif tag == "service":
                            exp_svc.append(item)
                        elif tag == "receiver":
                            exp_recv.append(item)
                        else:
                            item["authorities"] = _a(el, "authorities")
                            item["grant_uri"] = _a(el, "grantUriPermissions")
                            exp_svc.append(item) if False else None
                            comps.setdefault("exported_providers", []).append(item)
                if tag == "data":
                    scheme, host, pfx = _a(el, "scheme"), _a(el, "host"), _a(el, "pathPrefix")
                    if scheme:
                        schemes.add(scheme)
                    if scheme or host:
                        links.append({"scheme": scheme, "host": host, "path": pfx})
        except Exception as e:
            comps["_manifest_walk_error"] = f"{type(e).__name__}: {e}"
        comps["deep_links"] = links[:80]
        comps["custom_schemes"] = sorted(schemes)
        comps["exported"] = {"activities": exp_acts, "services": exp_svc, "receivers": exp_recv}

        for c in a.get_certificates():
            out["certificates"].append({
                "subject": getattr(getattr(c, "subject", None), "human_friendly", str(c.subject)),
                "issuer": getattr(getattr(c, "issuer", None), "human_friendly", str(c.issuer)),
                "serial": str(c.serial_number),
                "sha1": c.sha1.hex() if hasattr(c, "sha1") else None,
                "sha256": c.sha256.hex() if hasattr(c, "sha256") else None,
                "not_before": str(getattr(c, "not_valid_before", "")),
                "not_after": str(getattr(c, "not_valid_after", "")),
                "self_signed": getattr(c, "self_signed", None),
            })
    except Exception as e:  # androguard missing or parse failure
        out["manifest"]["_androguard_error"] = f"{type(e).__name__}: {e}"

    # ---- interesting literals (keys, hints, api paths)
    interesting = set()
    for pat in (rb"/api/[A-Za-z0-9_\-/]{2,60}", rb"[A-Za-z0-9_\-]{3,40}(?:/v[12])/[A-Za-z0-9_\-/]{2,40}",
                rb"X-[A-Za-z0-9-]{3,30}", rb"[A-Za-z0-9_]*KEY[A-Za-z0-9_]*", rb"[A-Za-z0-9_]*TOKEN[A-Za-z0-9_]*"):
        for m in re.findall(pat, dex_blob):
            s = m.decode("ascii", "ignore")
            if 4 < len(s) < 70:
                interesting.add(s)
    out["strings_of_interest"] = sorted(interesting)[:400]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apk")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    res = recon(Path(args.apk))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2, ensure_ascii=False))
        print(f"wrote {args.json}")
    else:
        print(json.dumps(res, indent=2, ensure_ascii=False)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
