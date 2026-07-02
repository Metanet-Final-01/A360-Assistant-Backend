# A360 Backend AWS Deployment

This repo keeps the backend small and deploys it as a Docker image behind AWS infrastructure.

## Target Layout

- Public subnets: Application Load Balancer, NAT Gateway
- Private app subnets: EC2 Auto Scaling Group running the FastAPI backend container
- Private data subnets: RDS PostgreSQL and optional OpenSearch
- Storage: S3 buckets for documents and logs
- Secrets: Secrets Manager for DB credentials, OpenAI key, and optional GHCR token
- Access: SSM Session Manager instead of public SSH

EC2 and DB are intentionally private. The backend still needs outbound internet for GHCR, OpenAI, package/image pulls, and external APIs, so the template includes one NAT Gateway.

## CloudFormation

Template:

```text
cloudformation/a360-backend-private.yml
```

`cloudformation/params.example.json` is only a reference for manual deployment. Do not treat it as the source of truth for application settings or secrets.

Example deploy:

```powershell
aws cloudformation deploy `
  --region ap-northeast-2 `
  --stack-name a360-assistant-dev `
  --template-file cloudformation/a360-backend-private.yml `
  --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides `
    Environment=dev `
    ContainerImage=ghcr.io/OWNER/REPO/backend:latest `
    FrontendOrigins=http://localhost:5173,https://your-vue-domain.example.com `
    OpenAiApiKey=replace-me `
    EnableOpenSearch=false
```

Before a real deploy, replace at least:

```text
ContainerImage
FrontendOrigins
```

`ContainerImage` must point to an image that EC2 can pull. Public GHCR images work without a token. Private GHCR images need the `GithubToken` CloudFormation parameter.

## Configuration Rule

Use each config surface for a different job:

```text
.env.example                 Local development contract only
GitHub Secrets/Variables     CI/CD deploy inputs
Secrets Manager              Runtime secrets on AWS
CloudFormation parameters    Infrastructure switches and image tag
```

Avoid copying the same value into both `.env` and `params.example.json`. For example, `OPENAI_API_KEY` should be in local `.env` for local runs, in GitHub Secrets for deploy, and in AWS Secrets Manager at runtime. It should not be committed into a CloudFormation params file.

Good CloudFormation parameters:

```text
Environment
ContainerImage
FrontendOrigins
EnableOpenSearch
InstanceType
DesiredCapacity
MaxCapacity
CertificateArn
```

Poor CloudFormation params-file values:

```text
OPENAI_API_KEY
DATABASE_PASSWORD
GITHUB_TOKEN
JWT_SECRET
```

Those should come from GitHub Secrets or AWS Secrets Manager.

## PostgreSQL pgvector

The template provisions private RDS PostgreSQL. Enable pgvector from the application migration or a one-time admin migration:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Keep PostgreSQL in private data subnets. Do not expose port `5432` publicly.

## OpenSearch

`EnableOpenSearch=false` by default because managed OpenSearch has a noticeable fixed cost. Turn it on when full-text search is actually needed:

```text
EnableOpenSearch=true
```

The OpenSearch domain is private inside the VPC and accepts traffic only from the app security group.

## GitHub Actions

Workflow:

```text
.github/workflows/backend-deploy.yml
```

It builds and pushes:

```text
ghcr.io/<owner>/<repo>/backend:<commit-sha>
ghcr.io/<owner>/<repo>/backend:latest
```

For CloudFormation deployment, configure these GitHub secrets:

```text
AWS_DEPLOY_ROLE_ARN
FRONTEND_ORIGINS
OPENAI_API_KEY
```

Optional repository variable:

```text
ENABLE_OPENSEARCH=false
```

## Next Architecture Steps

- Vue frontend: deploy separately to Vercel or S3 + CloudFront.
- Airflow: add a separate private ETL EC2 or move later to MWAA.
- Monitoring: add a small monitoring EC2 or use CloudWatch first, then Prometheus/Grafana/Loki when metrics requirements are clearer.
- RAG workers: split from the backend container once the LangGraph/TensorFlow/PDFBox code is ready.
- WAF/ACM/Route 53: attach after the API domain is decided.
