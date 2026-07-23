# Backend Deploy Test

Use this for short backend deployment tests after the shared network/data stacks are already deployed.

## GitHub Actions

Run `.github/workflows/backend-deploy.yml` manually with:

- `deploy_stack`: `true`
- `instance_type`: `t3.small`
- `desired_capacity`: `1`
- `max_capacity`: `1`
- `start_backend_container`: `true`

For the first DB bootstrap deployment only, set `start_backend_container` to `false`. In that mode the EC2 instance stays reachable through SSM and is not registered in the ALB target group.

The workflow uses `backend-deploy-${environment}` as the GitHub Actions environment and `a360-assistant-${environment}-backend` as the stack name.

## Local CloudFormation Params

Use `infra/params.deploy-test-cheap.example.json` as the starting point for local `aws cloudformation deploy` parameter overrides.

