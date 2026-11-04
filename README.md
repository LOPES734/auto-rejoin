# Roblox Auto Rejoin

Programa para Windows 10/11 que vigia o processo do Roblox (`RobloxPlayerBeta.exe`) e, se o jogo fechar inesperadamente, tenta voltar a abrir a experiência escolhida através do seu **Place ID**.

- Não abre o jogo ao arrancar: só começa a vigiar quando carregas em **Iniciar**.
- Não pede nem guarda palavras-passe, cookies ou tokens do Roblox (a única credencial opcional é o URL do webhook do Discord, ver abaixo). Não injeta código, não altera o cliente e não contorna o anti-cheat. Só pede ao Windows para abrir um link normal da experiência, como se o abrisses tu (e, se a internet cair ou o Roblox ficar desligado do servidor, fecha o processo do Roblox para o reabrir; ver “Se a internet cair” e “Se o Roblox for desligado do servidor”).

## Como obter o Place ID

Abre a página da experiência em `roblox.com`. O endereço tem o formato `https://www.roblox.com/games/`**`123456789`**`/Nome-do-jogo`. O número é o Place ID. Cola apenas esse número (sem o link) no campo **Place ID**.

> **Aviso importante:** o Place ID leva-te à experiência, mas **o Roblox escolhe o servidor**. Podes entrar num servidor diferente daquele em que estavas. Além disso, o programa só consegue confirmar que **o processo do Roblox abriu**; não consegue saber se entraste mesmo no servidor.

## Utilização (versão `.exe`)

1. Faz duplo clique em `RobloxAutoRejoin.exe`.
2. Escreve o **Place ID** e, se quiseres, o máximo de tentativas.
3. Carrega em **Iniciar**. Podes minimizar a janela; a vigilância continua em segundo plano.
4. Carrega em **Parar** quando quiseres terminar.

Se o Windows SmartScreen avisar que a aplicação é desconhecida, é porque o `.exe` não está assinado digitalmente: escolhe "Mais informações" → "Executar mesmo assim" (só se tiveres compilado o ficheiro tu próprio ou confiares na origem).

**Atenção:** se fechares o Roblox de propósito enquanto a vigilância está ativa, o programa vai tentar reabri-lo. Carrega em **Parar** antes de sair do jogo.

### Estados

| Estado | Significado |
|---|---|
| A aguardar | O Roblox não está aberto; o programa espera (não abre nada). |
| Roblox detetado | O processo está aberto e a ser vigiado. |
| Sem internet | Não há ligação à internet. O programa espera (não gasta tentativas) e age quando ela voltar. |
| A tentar reentrar | O processo desapareceu; a espera/tentativa de reabrir está em curso. |
| Erro | Dados inválidos ou limite de tentativas atingido. Ver a mensagem a vermelho (podes copiá-la). |
| Parado | A vigilância não está ativa. |

### Como funciona a reentrada

1. O processo desaparece → espera `rejoin_delay` segundos (por defeito 5).
2. Confirma que o Roblox continua fechado e envia o pedido de abertura. Se houver uma **Pasta do Roblox** configurada (por defeito `C:\\Users\\PC\\AppData\\Local\\Roblox\\Versions\\version-9ac8595712b447a7`), tenta primeiro executar o Roblox dessa pasta (`RobloxPlayerLauncher.exe` ou, se não existir, `RobloxPlayerBeta.exe`), passando-lhe o link `roblox://placeId=<ID>`. Se isso falhar, usa os links: em tentativas ímpares usa `https://www.roblox.com/games/start?placeId=<ID>` (abre o navegador, que lança o Roblox); em tentativas pares usa `roblox://placeId=<ID>`. Se um dos dois falhar com erro, tenta o outro na mesma tentativa.
3. Espera até `launch_wait` segundos (por defeito 60) que o processo apareça. Se aparecer, passa a "Roblox detetado".
4. Se não aparecer, faz uma pausa de `retry_interval` segundos (por defeito 10) e tenta de novo.
5. Ao atingir o máximo de tentativas, **para** e mostra um erro (nunca fica em ciclo infinito).
6. O contador de tentativas só volta a 0 depois de o Roblox estar aberto sem interrupções durante `stable_session` segundos (por defeito 120).

> Os formatos de link seguem a documentação oficial do Roblox ("Deep links", em create.roblox.com). Essa documentação indica que este mecanismo está em desuso em favor dos links de partilha, por isso, se o Roblox deixar de o aceitar, será preciso atualizar `build_urls()` em `monitor.py`.

## Várias contas (Roblox Account Manager)

Opcional, desligado por defeito. Com a caixa **Modo várias contas (Roblox Account Manager)** ligada, o programa passa a gerir **várias contas** ao mesmo tempo, cada uma com a sua vigilância própria (fechou, internet, desligado do servidor). Ao contrário do modo normal, **ao carregar em Iniciar ele abre as contas da lista** (uma de cada vez, com uma pausa entre elas).

**Como funciona**

1. Pede ao RAM para lançar cada conta pela API local dele, com o mesmo pedido do wrapper oficial do RAM: `http://localhost:<porta>/LaunchAccount?Account=<conta>&PlaceId=<id>` (mais `&JobId=…` e `&Password=…` se os definires).
2. Antes e depois de cada lançamento compara a lista de processos `RobloxPlayerBeta.exe` e a lista de ficheiros `.log` do Roblox: o processo e o log **novos** pertencem a essa conta. É assim que sabe qual é qual (o Roblox não o diz). Por isso os lançamentos são **um de cada vez**.
3. Se uma conta fechar ou for desligada do servidor, **só o processo dessa conta** é fechado e reaberto. As outras não são tocadas. Se a internet cair, cada conta é tratada por si (e os relançamentos são feitos em fila, não todos de uma vez).
4. O webhook mostra o campo **Conta** em cada mensagem.

**O que tens de fazer no RAM**

- Ligar o **servidor web/API** do RAM nas definições dele (o nome exato do menu varia entre versões). A porta por defeito é `7963`; se a mudares, muda-a aqui também. Carrega em **Testar RAM** para ver se responde.
- Ligar o **Multi Roblox** (vem desligado por defeito), senão só uma conta consegue ficar aberta.
- Se o RAM tiver uma opção para permitir o `LaunchAccount` na API, ligá-la. Se tiveres definido uma palavra-passe na API, escreve-a aqui (o RAM original só a usa com 6 ou mais caracteres).
- **Desligar a reentrada automática do próprio RAM** para estas contas, senão os dois programas abrem a mesma conta ao mesmo tempo.
- Fechar todos os Roblox abertos **antes** de carregar em Iniciar. O programa só gere as contas que ele próprio lançou; os processos que já lá estavam não são adotados (aparece um aviso no log).

**Lista de contas** (uma por linha): `nome no RAM, PlaceId, JobId`

```
alpha, 8304191830
beta, 8304191830
gamma, 8304191830, 0123abcd-4567-89ef-0123-456789abcdef
# linhas com # são ignoradas
```

O `PlaceId` pode ser omitido (usa-se o campo *Place ID* da janela). O nome tem de ser **exatamente** o da conta no RAM. O `JobId` (opcional) leva a conta para um servidor específico; um link de servidor privado também é aceite, mas **esse caso não está confirmado**.

**Segurança**

- O programa **nunca** pede cookies das contas: só chama `LaunchAccount` (qualquer outro método do RAM, como `GetCookie`, está bloqueado no código) e só fala com `localhost`.
- A palavra-passe da API do RAM é um segredo: fica escondida no campo e nunca aparece nos logs, mas é guardada **em texto simples** no `config.json`. Não partilhes esse ficheiro.
- O RAM guarda os cookies das tuas contas. Descarrega-o só do repositório oficial; circulam versões falsas com malware. O RAM original (ic3w0lf22) está **arquivado desde outubro de 2024**; os forks podem ter uma API diferente.

> **Limites importantes:** isto foi escrito a partir do código público do RAM original e **nunca foi testado com um RAM nem com Roblox reais** (os testes usam um RAM e um Roblox simulados). Os pontos mais incertos: se o teu RAM aceita o pedido tal como o escrevi, se cria um log novo por conta que o programa consegue associar (se não encontrar o log de uma conta, avisa e só deteta o fecho do processo, não o ecrã *Disconnected* dessa conta), e o comportamento com links de servidor privado. Começa com **duas contas** e vê o log do programa antes de usar mais.

## Se o Roblox for desligado do servidor (erro 277)

Ligado por defeito (caixa **Reentrar se o Roblox for desligado do servidor**). Quando o Roblox mostra o ecrã **Disconnected (Error Code: 277)**, o processo continua aberto, por isso só olhar para o processo não chega. O programa **lê o log do próprio Roblox** (`%LOCALAPPDATA%\Roblox\logs`, só leitura; também procura a versão da Microsoft Store) e, se aparecer um desligado com um dos códigos configurados, **fecha o Roblox à força** e reentra pelo fluxo normal.

- Códigos por defeito (`disconnect_codes` no `config.json`): `277` (perdeu a ligação), `278` (inativo), `279` (falhou a ligar), `274` (servidor encerrado). **Não** inclui `273` (conta aberta noutro dispositivo) nem `267`/`268` (expulso): reentrar nesses casos só provocaria um ciclo de expulsões.
- Só lê o log da sessão atual (ignora logs de sessões anteriores) e um log que já provocou uma reentrada não é lido outra vez.
- Cada reentrada gasta uma tentativa, por isso há sempre um limite (nunca fica em ciclo infinito).
- Se a pasta de logs estiver noutro sítio, indica-a em `roblox_log_dir`.

> **Limite importante:** escrevi esta deteção sem ter acesso a um log real do Roblox; os padrões (“disconnect”, “connection lost”, “error code” seguidos do número) são uma estimativa do formato. Quando fores testar, vê no log do programa a linha **“O Roblox foi desligado do servidor (erro 277)…”**. Se o Roblox mostrar o ecrã Disconnected e o programa **não reagir**, copia o ficheiro mais recente de `%LOCALAPPDATA%\Roblox\logs` e envia-o para ajustar os padrões.

## Se a internet cair

Ligado por defeito (caixa **Reentrar se a internet cair**). Quando a internet cai, o Roblox costuma ficar aberto num ecrã de “desligado” (o processo não desaparece), por isso o programa não repararia. Assim:

1. **A internet cai com o Roblox aberto:** o estado passa a **Sem internet**. Se a ligação voltar depois de ter estado em baixo **pelo menos 30 s** (configurável no campo *…em baixo há mais de (s)*), espera 10 s com a ligação estável e depois **fecha o Roblox à força** (só o `RobloxPlayerBeta.exe`) e reentra pelo fluxo normal. Quedas mais curtas são ignoradas: não mexe no Roblox.
2. **O Roblox fecha e não há internet:** não gasta tentativas nem abre nada; espera pela ligação (até `net_wait_max`, 1 hora por defeito; depois para com erro) e só então reabre.

A internet é verificada a cada *Verificar a cada* segundos (por defeito 3 s), abrindo uma ligação TCP a `www.roblox.com:443`, `1.1.1.1:443` e `8.8.8.8:443`; basta um responder. Só se declara “sem internet” após 2 falhas seguidas.

> **Atenção:** o limite de 30 s é uma estimativa de quanto tempo o Roblox demora a desligar-se do servidor; o programa **não consegue ver** o ecrã do Roblox. Se a tua internet cair por mais de 30 s mas o Roblox se reconectar sozinho, será fechado e reaberto mesmo assim. Se não quiseres isso, aumenta o valor ou desliga a caixa. Os campos `net_restore_delay` (espera após a ligação voltar, 10 s) e `net_wait_max` só se alteram no `config.json`.

## Notificações por webhook (Discord)

Opcional. Se colares o URL de um webhook do Discord no campo **Webhook Discord**, o programa envia uma mensagem quando:

| Evento | Mensagem |
|---|---|
| O processo do Roblox desaparece | ⚠️ O Roblox fechou ou crashou (com a duração da sessão) |
| A internet volta (depois de uma queda) | 🌐 A internet voltou (com o tempo que esteve em baixo) |
| O Roblox fica desligado do servidor (processo aberto) | 🔌 O Roblox foi desligado do servidor (com o código do erro) |
| O Roblox volta a abrir | ✅ O Roblox voltou a abrir (com o nº de tentativas e o método usado) |
| Esgotam-se as tentativas ou há um erro inesperado | ❌ Não foi possível reabrir o Roblox (com o motivo) |

Como criar o webhook: no Discord, canal → **Editar canal** → **Integrações** → **Webhooks** → **Novo webhook** → **Copiar URL do webhook**. Carrega em **Testar** para enviar uma mensagem de teste.

Notas:

- O programa **não consegue distinguir um crash de um fecho manual**: só vê que o processo desapareceu. Por isso a mensagem diz "fechou ou crashou".
- Não é enviada nenhuma mensagem ao arrancar nem ao carregar em Parar.
- Os envios correm numa thread à parte: se o Discord estiver em baixo ou sem ligação, regista um aviso no log e a vigilância continua normalmente.
- Só são aceites URLs `https://discord.com/api/webhooks/...` (também `discordapp.com`, `ptb.` e `canary.`).
- **O URL do webhook é um segredo** (quem o tiver pode enviar mensagens para o teu canal). Fica escondido no campo, nunca aparece nos logs, mas é guardado **em texto simples** no `config.json`. Não partilhes esse ficheiro; se o URL vazar, apaga o webhook no Discord e cria outro.

## Onde ficam a configuração e os logs

| Versão | Pasta |
|---|---|
| `.exe` normal | `%APPDATA%\RobloxAutoRejoin\` (`config.json`, `logs.txt`) |
| `.exe` portátil | subpasta `data` ao lado do `.exe` |
| Python (`py main.py`) | a própria pasta do projeto |

O botão **Abrir pasta de dados** abre essa pasta. O `config.json` é criado automaticamente com estes valores:

```json
{
  "place_id": "",
  "max_attempts": 5,
  "poll_interval": 3.0,
  "rejoin_delay": 5.0,
  "retry_interval": 10.0,
  "launch_wait": 60.0,
  "stable_session": 120.0,
  "launch_method": "auto",
  "roblox_path": "C:\\Users\\PC\\AppData\\Local\\Roblox\\Versions\\version-9ac8595712b447a7",
  "webhook_url": "",
  "net_rejoin": true,
  "net_down_threshold": 30.0,
  "net_restore_delay": 10.0,
  "net_wait_max": 3600.0,
  "disconnect_rejoin": true,
  "disconnect_codes": "277,278,279,274",
  "roblox_log_dir": "",
  "ram_enabled": false,
  "ram_port": 7963,
  "ram_password": "",
  "ram_accounts": "",
  "ram_launch_gap": 8.0,
  "ram_launch_timeout": 60.0
}
```

`launch_method` aceita `auto` (pasta, se definida, e depois alterna os links), `web`, `protocol` ou `exe` (só a pasta). `roblox_path` é a pasta `version-...` (ou o `.exe`); deixa vazio para usar só os links.

> **Atenção:** o nome da pasta `version-...` muda sempre que o Roblox atualiza. Quando isso acontecer, o programa regista o erro no log e usa os links como alternativa; atualiza o campo **Pasta do Roblox** quando quiseres. Não consegui testar se o executável do Roblox aceita o link `roblox://` como argumento neste ambiente (não é Windows): confirma no log se o processo aparece. Se o ficheiro ficar corrompido, é guardado como `config.json.bad` e é criado um novo.

## Executar com Python (desenvolvimento)

No PowerShell, dentro da pasta do projeto:

```powershell
py -m pip install -r requirements.txt
py main.py
```

Ou faz duplo clique em `start.bat`. Requer Python 3.9 ou superior.

## Criar o `.exe`

Faz duplo clique em `build.bat` (ou executa-o no PowerShell). O script:

1. instala as dependências (incluindo o PyInstaller);
2. corre os testes automatizados;
3. compila com `--onefile --windowed` (sem janela de consola);
4. arranca o `.exe` com `--selftest` para confirmar que carrega a configuração, escreve logs, lista processos e cria a janela;
5. cria a versão portátil.

Resultado:

- `dist\RobloxAutoRejoin.exe`
- `dist\RobloxAutoRejoin_Portable\` e `dist\RobloxAutoRejoin_Portable.zip` (o `.exe` + um ficheiro `portable.flag`)

O `.exe` inclui o Python e as bibliotecas; **não precisa de Python instalado** para ser usado. Para uma instalação "portátil", copia a pasta `RobloxAutoRejoin_Portable` para onde quiseres (mantém o `portable.flag` ao lado do `.exe`).

## Testes automatizados

```powershell
py -m unittest discover -s tests -v
```

Cobrem (incluindo o modo várias contas: lista de contas, pedido ao RAM, isolamento entre contas, lançamentos em fila, erros de uma conta sem parar as outras, palavra-passe fora dos logs; a deteção de desligado pelo log: códigos certos/errados, linhas incompletas, logs antigos, sem re-disparos; a internet: queda longa/curta, Roblox fechado sem internet, espera máxima, parar durante a espera; e o webhook: validação do URL, formato do pedido, ordem e falhas de envio, mensagens em cada evento): Roblox aberto normalmente, fechado manualmente, processo que não aparece após uma tentativa, Place ID inválido, limite de tentativas, botão Parar (a monitorizar, durante a espera e durante a abertura), reposição do contador após sessão estável, erros de permissões/processos que desaparecem, configuração e logs.

**Limites destes testes:** usam um Roblox e um abridor simulados, e o webhook é testado com um servidor local e um enviador falso (nada é enviado ao Discord a sério; usa o botão **Testar** para confirmar o teu webhook), e a interface é testada com um `customtkinter` falso. Provam a lógica, mas **não** o Roblox real, o comportamento do Windows com os links `https://`/`roblox://`, nem o aspeto visual da janela.

## Estrutura do projeto

| Ficheiro | Função |
|---|---|
| `main.py` | Ponto de entrada e interface (`customtkinter`) |
| `monitor.py` | Configuração, logs, deteção do processo, lógica de reentrada e webhook |
| `ramapi.py` | Lista de contas e cliente da API local do RAM (só `LaunchAccount`, só `localhost`) |
| `multi.py` | Modo várias contas: lança as contas, associa processo/log a cada uma e vigia cada uma |
| `config.json` | Configuração (criada automaticamente) |
| `logs.txt` | Registo de eventos (criado automaticamente) |
| `requirements.txt` | Dependências de execução (`psutil`, `customtkinter`) |
| `requirements-build.txt` | Dependência de compilação (`PyInstaller`) |
| `build.bat` / `start.bat` | Compilar o `.exe` / iniciar a versão Python |
| `tests/` | Testes automatizados |

Alterações face ao pedido: `requirements-build.txt` separa o PyInstaller das dependências de execução, e a lógica ficou em `monitor.py` sem qualquer código de interface para poder ser testada sem janela.

## Resolução de problemas

- **"Place ID inválido":** usa só dígitos, sem links, letras ou espaços.
- **O Roblox não abre:** confirma que o Roblox abre a experiência quando colas manualmente o link `https://www.roblox.com/games/start?placeId=<ID>` no navegador. Se não abrir à mão, o programa também não conseguirá.
- **Abre o navegador mas não o jogo:** o navegador pode pedir permissão para abrir o Roblox; aceita e marca "permitir sempre" para esse site.
- **Limite de tentativas atingido:** verifica a ligação, a conta com sessão iniciada e se a experiência está acessível; depois carrega em Iniciar.
- Usa **Copiar último erro** ou **Copiar logs** para partilhar o problema.
