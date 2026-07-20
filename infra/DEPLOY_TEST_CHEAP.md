# Cheap Deploy Test

Use this mode for short backend deployment tests where keeping the RDS backup/snapshot behavior matters, but NAT Gateway cost should be avoided.

## GitHub Actions

Run `.github/workflows/backend-deploy.yml` manually with:

- `deploy_stack`: `true`
- `network_mode`: `public-lite`
- `instance_type`: `t3.small`
- `desired_capacity`: `1`
- `max_capacity`: `1`
- `ENABLE_OPENSEARCH` repository variable: `false`

`public-lite` still keeps the backend behind the ALB security group. The app instances are placed in public subnets only so they can pull Docker images and call external APIs without a NAT Gateway.

## Local CloudFormation Params

Use `infra/params.deploy-test-cheap.example.json` as the starting point for local `aws cloudformation deploy` parameter overrides.

The normal/private configuration remains available with:

- `NetworkMode=private`
- `InstanceType=t3.medium`
- `MaxCapacity=2`

