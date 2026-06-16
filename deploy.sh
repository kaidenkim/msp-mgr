#!/bin/bash
set -e
INSTANCE="i-0a7fc7068c3acbf73"
BUCKET="kep-sre-config"
REGION="ap-northeast-2"
PREFIX="msp-manager"

echo "=== S3 업로드 ==="
FILES=(main.py server.py auth.py config.py requirements.txt)
for f in "${FILES[@]}"; do
  aws s3 cp "$f" "s3://$BUCKET/$PREFIX/$f" --region $REGION
done
aws s3 cp gcp/gcp.py "s3://$BUCKET/$PREFIX/gcp/gcp.py" --region $REGION
aws s3 cp gcp/billing.py "s3://$BUCKET/$PREFIX/gcp/billing.py" --region $REGION
aws s3 cp gcp/export.py "s3://$BUCKET/$PREFIX/gcp/export.py" --region $REGION
aws s3 cp gcp/constants.py "s3://$BUCKET/$PREFIX/gcp/constants.py" --region $REGION
aws s3 cp gcp/__init__.py "s3://$BUCKET/$PREFIX/gcp/__init__.py" --region $REGION
aws s3 cp aws/__init__.py "s3://$BUCKET/$PREFIX/aws/__init__.py" --region $REGION
aws s3 cp aws/services/__init__.py "s3://$BUCKET/$PREFIX/aws/services/__init__.py" --region $REGION
for svc in aws_session cmdb cost_explorer organizations organizations_tree resource_collector; do
  aws s3 cp "aws/services/${svc}.py" "s3://$BUCKET/$PREFIX/aws/services/${svc}.py" --region $REGION
done
aws s3 cp static/index.html "s3://$BUCKET/$PREFIX/static/index.html" --region $REGION

echo "=== presigned URL 생성 ==="
MAIN_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/main.py" --expires-in 600 --region $REGION)
SERVER_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/server.py" --expires-in 600 --region $REGION)
AUTH_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/auth.py" --expires-in 600 --region $REGION)
CONFIG_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/config.py" --expires-in 600 --region $REGION)
REQ_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/requirements.txt" --expires-in 600 --region $REGION)
GCP_GCP_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/gcp/gcp.py" --expires-in 600 --region $REGION)
GCP_BILLING_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/gcp/billing.py" --expires-in 600 --region $REGION)
GCP_EXPORT_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/gcp/export.py" --expires-in 600 --region $REGION)
GCP_CONST_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/gcp/constants.py" --expires-in 600 --region $REGION)
GCP_INIT_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/gcp/__init__.py" --expires-in 600 --region $REGION)
AWS_INIT_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/__init__.py" --expires-in 600 --region $REGION)
AWS_SVC_INIT_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/__init__.py" --expires-in 600 --region $REGION)
AWS_SESSION_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/aws_session.py" --expires-in 600 --region $REGION)
AWS_CMDB_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/cmdb.py" --expires-in 600 --region $REGION)
AWS_CE_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/cost_explorer.py" --expires-in 600 --region $REGION)
AWS_ORG_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/organizations.py" --expires-in 600 --region $REGION)
AWS_ORGT_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/organizations_tree.py" --expires-in 600 --region $REGION)
AWS_RC_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/aws/services/resource_collector.py" --expires-in 600 --region $REGION)
INDEX_URL=$(aws s3 presign "s3://$BUCKET/$PREFIX/static/index.html" --expires-in 600 --region $REGION)

echo "=== EC2 배포 ==="
aws ssm send-command \
  --instance-ids "$INSTANCE" \
  --document-name "AWS-RunShellScript" \
  --region $REGION \
  --parameters "commands=[
    \"mkdir -p /home/ec2-user/msp-manager/gcp /home/ec2-user/msp-manager/aws/services /home/ec2-user/msp-manager/static /home/ec2-user/msp-manager/data\",
    \"cd /home/ec2-user/msp-manager\",
    \"curl -s -o main.py '$MAIN_URL'\",
    \"curl -s -o server.py '$SERVER_URL'\",
    \"curl -s -o auth.py '$AUTH_URL'\",
    \"curl -s -o config.py '$CONFIG_URL'\",
    \"curl -s -o requirements.txt '$REQ_URL'\",
    \"curl -s -o gcp/__init__.py '$GCP_INIT_URL'\",
    \"curl -s -o gcp/gcp.py '$GCP_GCP_URL'\",
    \"curl -s -o gcp/billing.py '$GCP_BILLING_URL'\",
    \"curl -s -o gcp/export.py '$GCP_EXPORT_URL'\",
    \"curl -s -o gcp/constants.py '$GCP_CONST_URL'\",
    \"curl -s -o aws/__init__.py '$AWS_INIT_URL'\",
    \"curl -s -o aws/services/__init__.py '$AWS_SVC_INIT_URL'\",
    \"curl -s -o aws/services/aws_session.py '$AWS_SESSION_URL'\",
    \"curl -s -o aws/services/cmdb.py '$AWS_CMDB_URL'\",
    \"curl -s -o aws/services/cost_explorer.py '$AWS_CE_URL'\",
    \"curl -s -o aws/services/organizations.py '$AWS_ORG_URL'\",
    \"curl -s -o aws/services/organizations_tree.py '$AWS_ORGT_URL'\",
    \"curl -s -o aws/services/resource_collector.py '$AWS_RC_URL'\",
    \"curl -s -o static/index.html '$INDEX_URL'\",
    \"pip3 install -q -r requirements.txt\",
    \"lsof -ti :9070 | xargs kill -9 2>/dev/null || true\",
    \"sleep 2\",
    \"nohup python3 server.py >> /tmp/msp-manager.log 2>&1 &\",
    \"sleep 4\",
    \"curl -s http://localhost:9070/health || echo 'health check failed'\"
  ]" \
  --output text --query "Command.CommandId"
