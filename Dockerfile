FROM python:3.13-alpine

# Install system dependencies
RUN apk add --no-cache wget unzip git curl gnupg bash

# Install Terraform
ARG TERRAFORM_VERSION=1.8.5
RUN wget -q https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip && \
    unzip terraform_${TERRAFORM_VERSION}_linux_amd64.zip -d /usr/local/bin/ && \
    rm terraform_${TERRAFORM_VERSION}_linux_amd64.zip && \
    terraform version

# Install Checkov
RUN pip install --no-cache-dir checkov

# App setup
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Default: run the HTTP server. Override to run scheduler instead.
CMD ["python", "server.py"]

