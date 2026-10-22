# Telco Churn Analytics & Data Warehouse
# Owners & Developers: MuhammaD_Wallah and the GROUP

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (PostgreSQL client for schema setup and build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Ensure the entrypoint has Unix line endings and execute permissions
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

EXPOSE 8501

ENTRYPOINT ["./docker-entrypoint.sh"]
