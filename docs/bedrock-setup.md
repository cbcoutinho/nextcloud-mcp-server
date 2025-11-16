# Amazon Bedrock Setup Guide

This guide covers how to configure the Nextcloud MCP Server to use Amazon Bedrock for embeddings and text generation.

## Prerequisites

1. **AWS Account** with access to Amazon Bedrock
2. **boto3 library** installed: `pip install boto3` or `uv sync --group dev`
3. **Model Access** - Request access to models in AWS Bedrock console

## Required AWS Permissions

### IAM Policy for Bedrock Access

The AWS IAM user or role needs the following permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvokeModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/*"
      ]
    }
  ]
}
```

### Minimal Permissions (Production)

For production deployments, restrict to specific models:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockEmbeddings",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    },
    {
      "Sid": "BedrockGeneration",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel"
      ],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"
      ]
    }
  ]
}
```

### Additional Permissions (Optional)

For advanced use cases:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockListModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:GetFoundationModel"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BedrockAsyncInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModelAsync",
        "bedrock:GetAsyncInvoke",
        "bedrock:ListAsyncInvokes"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/*"
      ]
    }
  ]
}
```

## Model Access

Before using Bedrock models, you must request access in the AWS Console:

1. Navigate to **Amazon Bedrock** → **Model access**
2. Click **Manage model access**
3. Select models you want to use:
   - **Embeddings:** Amazon Titan Embed Text, Cohere Embed
   - **Text Generation:** Anthropic Claude, Meta Llama, Amazon Titan Text
4. Click **Request model access**
5. Wait for approval (usually instant for most models)

## Supported Models

### Embedding Models

| Provider | Model ID | Dimensions | Best For |
|----------|----------|------------|----------|
| Amazon Titan | `amazon.titan-embed-text-v1` | 1,536 | General purpose |
| Amazon Titan | `amazon.titan-embed-text-v2:0` | 1,024 | Latest, improved quality |
| Cohere | `cohere.embed-english-v3` | 1,024 | English text |
| Cohere | `cohere.embed-multilingual-v3` | 1,024 | Multilingual |

### Text Generation Models

| Provider | Model ID | Context | Best For |
|----------|----------|---------|----------|
| Anthropic | `anthropic.claude-3-sonnet-20240229-v1:0` | 200K | Balanced performance |
| Anthropic | `anthropic.claude-3-haiku-20240307-v1:0` | 200K | Fast, cost-effective |
| Anthropic | `anthropic.claude-3-opus-20240229-v1:0` | 200K | Highest quality |
| Meta | `meta.llama3-8b-instruct-v1:0` | 8K | Fast, open-source |
| Meta | `meta.llama3-70b-instruct-v1:0` | 8K | High quality |
| Amazon | `amazon.titan-text-express-v1` | 8K | Fast, low cost |
| Mistral | `mistral.mistral-7b-instruct-v0:2` | 32K | Efficient |

## Configuration

### Environment Variables

**Required:**
```bash
AWS_REGION=us-east-1
```

**Optional (at least one model required):**
```bash
# For embeddings
BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0

# For text generation (RAG evaluation)
BEDROCK_GENERATION_MODEL=anthropic.claude-3-sonnet-20240229-v1:0
```

**AWS Credentials (choose one method):**

**Method 1: Environment Variables**
```bash
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

**Method 2: AWS Credentials File** (`~/.aws/credentials`)
```ini
[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

**Method 3: IAM Role** (when running on AWS EC2/ECS/Lambda)
- No credentials needed, uses instance/task role automatically

### Docker Configuration

Add to your `docker-compose.yml`:

```yaml
services:
  mcp:
    environment:
      - AWS_REGION=us-east-1
      - BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
      - BEDROCK_GENERATION_MODEL=anthropic.claude-3-sonnet-20240229-v1:0
      - AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
      - AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
```

Or use AWS credentials file volume mount:

```yaml
services:
  mcp:
    volumes:
      - ~/.aws:/root/.aws:ro
    environment:
      - AWS_REGION=us-east-1
      - BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
```

## Usage Examples

### Embeddings Only

```bash
export AWS_REGION=us-east-1
export BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret

uv run nextcloud-mcp-server
```

### Both Embeddings and Generation

```bash
export AWS_REGION=us-east-1
export BEDROCK_EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
export BEDROCK_GENERATION_MODEL=anthropic.claude-3-sonnet-20240229-v1:0

# For RAG evaluation with Bedrock
export RAG_EVAL_PROVIDER=bedrock
export RAG_EVAL_BEDROCK_MODEL=anthropic.claude-3-sonnet-20240229-v1:0

uv run python -m tests.rag_evaluation.evaluate
```

### Programmatic Usage

```python
from nextcloud_mcp_server.providers import BedrockProvider

# Embeddings only
provider = BedrockProvider(
    region_name="us-east-1",
    embedding_model="amazon.titan-embed-text-v2:0",
)

embeddings = await provider.embed_batch(["text1", "text2"])

# Both capabilities
provider = BedrockProvider(
    region_name="us-east-1",
    embedding_model="amazon.titan-embed-text-v2:0",
    generation_model="anthropic.claude-3-sonnet-20240229-v1:0",
)

# Generate embeddings
embedding = await provider.embed("query text")

# Generate text
response = await provider.generate("Write a summary", max_tokens=500)
```

## Cost Considerations

### Embedding Costs (as of Jan 2025)

| Model | Price per 1K tokens |
|-------|---------------------|
| Titan Embed Text v2 | $0.0001 |
| Cohere Embed English v3 | $0.0001 |

### Generation Costs (as of Jan 2025)

| Model | Input (per 1K tokens) | Output (per 1K tokens) |
|-------|----------------------|------------------------|
| Claude 3 Haiku | $0.00025 | $0.00125 |
| Claude 3 Sonnet | $0.003 | $0.015 |
| Claude 3 Opus | $0.015 | $0.075 |
| Llama 3 8B | $0.0003 | $0.0006 |
| Titan Text Express | $0.0002 | $0.0006 |

**Note:** Prices vary by region. Check [AWS Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/) for current rates.

## Troubleshooting

### Error: "Executable doesn't exist" or boto3 not found

**Solution:**
```bash
uv sync --group dev  # Installs boto3
```

### Error: "AccessDeniedException"

**Causes:**
1. IAM permissions missing
2. Model access not requested
3. Wrong AWS region

**Solution:**
1. Verify IAM policy includes `bedrock:InvokeModel`
2. Request model access in Bedrock console
3. Check model is available in your region

### Error: "ResourceNotFoundException"

**Cause:** Invalid model ID or model not available in region

**Solution:**
- Verify model ID matches exactly (case-sensitive)
- Check model availability in your AWS region
- Use `aws bedrock list-foundation-models` to see available models

### Error: "ThrottlingException"

**Cause:** Rate limit exceeded

**Solution:**
- Reduce request rate
- Request quota increase via AWS Support
- Use batch operations where possible

## Security Best Practices

1. **Use IAM Roles** when running on AWS infrastructure
2. **Rotate Access Keys** regularly if using IAM users
3. **Restrict Permissions** to only required models
4. **Enable CloudTrail** for audit logging
5. **Use AWS Secrets Manager** for credential management
6. **Monitor Costs** with AWS Cost Explorer and Budgets

## Regional Availability

Amazon Bedrock is available in:
- **US East (N. Virginia)**: `us-east-1` ✅ Most models
- **US West (Oregon)**: `us-west-2` ✅ Most models
- **Asia Pacific (Singapore)**: `ap-southeast-1`
- **Asia Pacific (Tokyo)**: `ap-northeast-1`
- **Europe (Frankfurt)**: `eu-central-1`

**Note:** Model availability varies by region. Check the [AWS Bedrock documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html) for current availability.

## References

- [AWS Bedrock Documentation](https://docs.aws.amazon.com/bedrock/)
- [AWS Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [boto3 Bedrock Runtime API](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/bedrock-runtime.html)
- [Provider Architecture ADR](./ADR-015-unified-provider-architecture.md)
