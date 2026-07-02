# Provisionamento de infraestrutura AWS com Python e boto3

Este projeto automatiza a criação de uma infraestrutura básica em AWS para hospedar uma aplicação web composta por:

- um backend FastAPI;
- um frontend React/Vite;
- um Application Load Balancer;
- um banco de dados PostgreSQL no RDS;
- um Auto Scaling Group para manter instâncias disponíveis.

O script principal, [main.py](main.py), cria uma arquitetura semelhante ao laboratório da AWS Academy, com VPC, subnets públicas e privadas, Internet Gateway, NAT Gateway, Security Groups, ALB, Launch Template, Auto Scaling Group e RDS PostgreSQL.

## O que a infraestrutura cria

A implementação provisiona os seguintes recursos:

- 1 VPC
- 2 subnets públicas
- 2 subnets privadas
- 1 Internet Gateway
- 1 NAT Gateway
- 2 route tables
- 3 security groups
- 1 Application Load Balancer
- 1 Launch Template
- 1 Auto Scaling Group
- 1 instância RDS PostgreSQL Multi-AZ

A aplicação é implantada nas instâncias EC2 através de user data, com:

- backend em container Docker exposto localmente na porta 8000;
- frontend compilado em arquivos estáticos;
- Nginx servindo o frontend e fazendo proxy de /api para o backend.

## Pré-requisitos

Antes de executar, certifique-se de que você possui:

- uma conta AWS ativa;
- permissões para criar recursos de EC2, ELB, RDS, VPC e Auto Scaling;
- Python 3 instalado;
- o pacote boto3 instalado;
- credenciais AWS válidas configuradas para o script.

## Instalação

1. Crie e ative um ambiente virtual (opcional, mas recomendado):

   ```bash
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

2. Instale as dependências:

   ```bash
   pip install -r requirements.txt
   ```

## Configuração

Edite o arquivo [main.py](main.py) e ajuste os valores principais, especialmente:

- região AWS;
- CIDRs das subnets;
- nome do banco e credenciais;
- tipo de instância EC2;
- perfil de instância;
- repositórios do backend e do frontend;
- credenciais AWS utilizadas na sessão boto3.

> Em ambientes de produção, prefira usar variáveis de ambiente, AWS Secrets Manager ou outro mecanismo seguro para armazenar credenciais.

## Quickstart

Rápido passo a passo para executar o provisionamento na sua máquina:

- Exporte ou configure suas credenciais AWS (via `aws configure` ou variáveis de ambiente):

   ```powershell
   aws configure
   # ou
   setx AWS_PROFILE "default"
   setx AWS_REGION "us-east-1"
   ```

- Opcional: defina variáveis sensíveis via variáveis de ambiente antes de executar:

   ```powershell
   setx DB_NAME "mydb"
   setx DB_USER "admin"
   setx DB_PASSWORD "sua_senha_segura"
   ```

- Execute o script principal:

   ```powershell
   python main.py
   ```

## Credenciais e variáveis de ambiente

- O script usa `boto3` e respeita o perfil e região configurados pelo AWS CLI (`AWS_PROFILE`, `AWS_REGION`) ou pelas variáveis de ambiente equivalentes.
- Não armazene senhas ou chaves diretamente no repositório. Use `AWS Secrets Manager`, `SSM Parameter Store` ou variáveis de ambiente para produção.

## Limpeza (teardown)

Para evitar custos, remova os recursos quando não estiver testando.

- O projeto inclui o script `restart.py` que pode destruir e opcionalmente recriar a infraestrutura. Para apenas destruir sem confirmação interativa:

   ```powershell
   python restart.py --destroy-only --yes
   ```

- Você também pode excluir recursos manualmente pelo AWS Console.

## Observações adicionais
- O `restart.py` encapsula lógica segura de remoção (aguarda término de instâncias, remoção ordenada de security groups, etc.). Use-o quando precisar reaplicar a infraestrutura de forma limpa.
- Aguarde alguns minutos para que RDS e instâncias EC2 finalizaram seus processos de criação/remoção.

## Execução

Execute o script com:

```bash
python main.py
```

Durante a execução, o script vai:

1. criar a infraestrutura na AWS;
2. provisionar o banco de dados RDS;
3. criar o template de lançamento das instâncias;
4. criar o Auto Scaling Group;
5. imprimir a URL pública do Application Load Balancer no final.

## Verificação

Após o provisionamento, teste a aplicação acessando a URL do ALB exibida no terminal. O fluxo esperado é:

- a página inicial do frontend ser carregada;
- o backend responder em /api;
- o health check do ALB funcionar corretamente.

No script atual, o health check do target group está configurado para a rota "/", que corresponde ao endpoint de saúde do backend.

## Limpeza

Os recursos criados não são removidos automaticamente pelo script. Para evitar custos desnecessários, remova os recursos manualmente pela AWS Console ou via AWS CLI.

## Observações

- O script foi pensado para laboratórios e ambientes didáticos, como o AWS Academy.
- Pode ser necessário esperar alguns minutos para que o RDS e as instâncias EC2 fiquem disponíveis.
- Se a aplicação ficar indisponível ou o ALB reportar falha no health check, verifique:
  - se o backend iniciou corretamente;
  - se o Nginx está rodando;
  - se o endpoint de saúde está respondendo na rota esperada.
