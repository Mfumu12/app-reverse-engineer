# SportzX — Dossiê de engenharia reversa

**Status:** `FASE 1 (registro público) — concluída` · `FASE 2 (binário) — bloqueada, aguardando arquivo`

Arquivo alvo: `SportzX_3.1v.apk` · 20.884.286 bytes (19,9 MB) · Google Drive `1-DimckqtxxeSBeAAQNGe8cA-5d6Oty9n`

> Nada neste documento veio do binário do usuário. Tudo aqui é registro público
> (sites de divulgação, listagens de terceiros) ou verificação feita por mim em
> fontes acessíveis. Os pontos marcados **[não verificado]** são alegações de
> terceiros e não devem ser tratados como fato até a Fase 2.

---

## 1. Por que a Fase 2 está bloqueada

O APK está no Drive e não é legível a partir deste ambiente. Causas medidas:

| Caminho | Resultado |
|---|---|
| `curl` do sandbox para `drive.google.com` | `SSL_ERROR_SYSCALL` — sem rota (só GitHub/PyPI são liberados) |
| Conector Google Drive (`download_file`) | devolve binário em **base64**: 19,9 MB → ~26,5 MB de texto, inviável no contexto |
| Espelho em GitHub (7 repos testados: `sportzxapk/sportzx`, `fireflies13118/sportzxapk`, `jalil00009/sportzxapk`, `zkk4555-ai/Sportzx`, `Sanskar318/Sportzx`, `sportzx/sportzx-android-tv-guides`, `emiaarlutay-design/SportzX`) | nenhum contém `.apk` em release ou na árvore; `zkk4555-ai/Sportzx` está vazio |

**Desbloqueio:** anexar o APK na conversa (cai no workspace) ou subir num repositório
GitHub ≤ 25 MB (acessível daqui). Nenhuma análise de binário é possível antes disso.

---

## 2. O que é o SportzX, segundo o registro público

- App Android de **agregação de esportes ao vivo** — não é um serviço de streaming
  próprio, e sim um cliente que reúne canais e eventos de terceiros numa interface
  de TV. Cobertura alegada: críquete, futebol, WWE/AEW, UFC, F1, tênis, kabaddi,
  NBA, NHL, rugby; e "200+ a 600+ canais" **[não verificado]**.
- **Distribuição exclusivamente por sideload** (APK). Não há publicação no Google
  Play, o que já elimina Play Integrity, revisão de malware e o sandbox de
  atualização automática.
- **Desenvolvedor não identificado** — as próprias páginas de divulgação descrevem
  como "independent developer (not publicly identified)".
- Modelo: **gratuito, sem conta, sem debrid**. Um review afirma que "não exige
  cadastro e abre direto na tela inicial", com suporte a playlists M3U próprias e
  favoritos.
- Ecossistema aparentado: citado como vindo "dos criadores do CricFy TV" e
  comparado a Cricfy TV, Sportzfy TV, RoyalX777 e similares — um clube de apps de
  redistribuição, não de emissoras.

### 2.1 Sobre a legalidade das fontes

A pergunta relevante não é se o app é grátis (é), e sim se os **sinais que ele
repassa** têm direito de transmissão no seu país. A própria imprensa especializada
que o divulga diz: "não se garante que todas as fontes de stream tenham direitos de
transmissão adequados, e isso varia por fonte e por país". Canais como Sky Sports,
Sony Sports, beIN e PTV são pay-TV — quando aparecem de graça num agregador, é
porque o sinal está sendo repassado por alguém sem licença para fazê-lo.

Consequência prática para o usuário: instalar é fácil, mas o risco jurídico e o
risco de malware recaem sobre quem instala, não sobre quem publicou.

---

## 3. Achado importante: não existe "um" SportzX

Durante a apuração apareceram versões e identidades **incompatíveis entre si** para
o mesmo nome:

| Fonte | Versão anunciada | Tamanho | Package declarado |
|---|---|---|---|
| `sportzxapk.app` | 3.2 | 13,6 MB | — |
| `sportzx.org` | 3.2.1 | — | — |
| `sportzxtvapk.net` | 3.2 | — | — |
| `sportzx-5.modfyp.com` | **15.0.0** | 8 MB | **`com.moviesandseries.moviebox`** |
| arquivo do usuário | 3.1v | 19,9 MB | — |

Três leituras disso, todas relevantes:

1. **`com.moviesandseries.moviebox`** é o package de um app de outro gênero
   (catálogo de filmes/séries, não esportes). Um "SportzX" com esse package é
   **rebrand de outro APK**, o padrão clássico de repack: pega-se um app existente,
   troca-se nome/ícone e redistribui-se.
2. Tamanhos que vão de 8 MB a 19,9 MB para o "mesmo" app indicam **builds
   diferentes**, não versões do mesmo binário — de 8 a 20 MB cabe muita coisa.
3. Nomes tipo `modfyp.com` são fazendas de APK modificado, cujo produto costuma
   incluir injeção de SDK de anúncio, proxy de tráfego e, em casos conhecidos,
   spyware bancário.

**Portanto: o hash e a assinatura do SEU arquivo são o dado mais importante deste
relatório.** É o que separa "o SportzX" de "um SportzX". É exatamente por isso que
a Fase 2 não pode ser substituída por nenhuma análise pública.

---

## 4. O que a Fase 2 entrega (assim que o binário chegar)

Ferramenta pronta e validada: `tools/apk_recon.py`, testada em APK real de app de
vídeo Android TV (11 MB, execução em ~13 s), extraindo:

- identidade: package, versão, min/target SDK, label resolvido, SHA-256/MD5
- assinatura: sujeito/emissor do certificado, validade, autoassinatura, SHA-1/SHA-256
  → identifica debug key, repack e reassinatura
- superfície: permissões (com as perigosas marcadas), activities/services/receivers/
  providers exportados, deep links, schemes customizados
- stack: player (ExoPlayer/Media3, VLC, ijk), ads, analytics, crash reporting,
  framework (nativo/Flutter/RN), arquiteturas nativas, DEX count
- rede: domínios, endpoints de API, hosts de mídia, IPs, e-mails, cabeçalhos
  customizados, uso de HTTP em claro
- segredos: chaves e tokens hardcoded com filtro de entropia
- riscos: achados com severidade e a entrada/arquivo de origem
- arquitetura: fluxo catálogo → resolução de fonte → player, e onde o controle de
  acesso se aplica

**Limite declarado:** o relatório descreve *como* o app funciona e *como* o acesso é
controlado. Não entrega pacote pronto de fontes/mídia para consumo fora do app —
isso seria substituir o produto de terceiros, não analisá-lo.

---

## 5. Recomendação antes de instalar

Enquanto a Fase 2 não roda, três cuidados que valem independentemente do resultado:

1. Não instale o APK em aparelho com banco, e-mail e 2FA. Use perfil separado ou
   aparelho secundário — sideload de origem desconhecida é o vetor mais comum de
   trojan Android.
2. Não conceda permissão de acessibilidade, de instalar apps, nem de administrador
   do dispositivo. Nenhum app de vídeo precisa disso; quando pede, é o golpe.
3. Se instalar, prefira DNS filtrado e não faça login em nada dentro do app.
