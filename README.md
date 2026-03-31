# bot-ia-cripto-slim

Projeto completo do **bot-ia-cripto** em arquitetura slim, com:

- contêiner único Docker
- FastAPI + painel web responsivo
- autenticação por sessão
- modo **simulado** por padrão
- camada **real Binance Spot pronta**
- coleta via REST + WebSocket público Binance
- análise social via ApeWisdom
- feed RSS de notícias
- rastreio completo em SQLite
- treinamento sob demanda com **Random Forest** e **XGBoost**
- hot reload de configuração via YAML

## Estrutura

```text
bot-ia-cripto-slim/
├── docker-compose.yml
├── .env.example
├── Dockerfile
├── README.md
├── requirements.txt
├── config/
│   ├── bot_config.yaml
│   ├── symbols.yaml
│   └── models/
├── data/
└── app/
    ├── __init__.py
    ├── main.py
    ├── db.py
    ├── utils.py
    ├── collector.py
    ├── analyzer.py
    ├── decision.py
    ├── executor.py
    ├── tracer.py
    ├── models.py
    ├── webui.py
    ├── templates/
    └── static/
```

## Como subir

### 1) Preparar ambiente
```bash
cp .env.example .env
```

Edite o `.env` se quiser deixar as chaves da Binance já configuradas.

### 2) Build
```bash
docker compose build
```

### 3) Subir
```bash
docker compose up -d
```

### 4) Acessar o painel
Abra no navegador:

```text
http://localhost:8080
```

Login inicial padrão:
- usuário: `admin`
- senha: `admin123`

## Operação

### Modo padrão
O projeto sobe em:

```yaml
general:
  trade_mode: simulated
```

Ou seja, a carteira inicial é simulada e persistida em `data/bot.db`.

### Trocar para real
Você pode:
- alterar no painel
- ou editar `config/bot_config.yaml`

> Observação: a camada real usa **Binance Spot** via `ccxt`, já preparada, mas o modo padrão fica em `simulated`.

## Principais telas

### Dashboard
- status do sistema
- patrimônio
- PnL
- posições abertas
- ordens recentes
- controles operacionais

### Configurações
- edição completa de `bot_config.yaml`
- edição de `symbols.yaml`
- alteração de senha do painel

### Trace View
- filtros por símbolo e nível
- histórico persistido
- stream em tempo real por WebSocket
- exportação JSON/CSV

### Treinamento
- criação de novos modelos
- escolha entre Random Forest e XGBoost
- ativação do modelo
- exclusão de modelos antigos

## Banco de dados

O banco é criado automaticamente em:

```text
data/bot.db
```

Tabelas criadas automaticamente:
- `traces`
- `candles`
- `features`
- `models_metadata`
- `simulated_balance`
- `simulated_orders`

## Observações técnicas

### Sobre SQLite
Nesta versão slim, o SQLite é adequado para:
- execução local
- VPS pequena
- ambiente pessoal
- baixa complexidade operacional

Para crescimento futuro, a camada está modularizada o suficiente para migrar depois para PostgreSQL.

### Sobre treinamento
O treinamento usa as features persistidas na tabela `features`. Para melhores resultados:

1. deixe o coletor rodar por algum tempo
2. atualize mercado/social
3. execute o treinamento na tela apropriada

### Sobre RSS e sentimento
A pontuação de notícias é configurável em `analysis.rss_non_english_fallback`, permitindo fallback neutro.

### Sobre Binance WebSocket
O bot tenta consumir `bookTicker` e `aggTrade`. Se houver indisponibilidade, ele continua operando com REST.

## Comandos úteis

### Ver logs
```bash
docker compose logs -f
```

### Reiniciar
```bash
docker compose restart
```

### Derrubar
```bash
docker compose down
```

### Resetar tudo
```bash
docker compose down
rm -f data/bot.db
docker compose up -d --build
```

## Próximas evoluções recomendadas

- adicionar backtesting dedicado
- incluir gráficos históricos no painel
- adicionar workers assíncronos para treino pesado
- migrar persistência para PostgreSQL em cenários maiores
- incluir estratégia de trailing stop e take profit
