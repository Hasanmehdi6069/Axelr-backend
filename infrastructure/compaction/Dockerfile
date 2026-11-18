# Production Compaction Container Layer: Alpine-based with PocketBase + Litestream for Cloudflare R2
# Build context: ./compaction (same directory as this Dockerfile)
FROM alpine:3.20.3 AS runtime

# Build arguments for pinned versions (immutable for production reproducibility)
ARG POCKETBASE_VERSION=0.22.18
ARG LITESTREAM_VERSION=0.3.13

# Environment configuration - all values pulled from runtime secrets, no hardcoding
ENV POCKETBASE_DIR=/app/pb_data
ENV LITESTREAM_CONFIG=/etc/litestream.yml
ENV DATA_PATH=/app/pb_data/data.db
ENV R2_BUCKET=s3://${R2_BUCKET_NAME}/litestream
ENV R2_ENDPOINT=${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com
ENV AWS_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
ENV AWS_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
ENV POCKETBASE_ENV=production
ENV POCKETBASE_HTTP_PORT=8090

# Install runtime dependencies: ca-certificates for TLS, tini for zombie reaping, curl for health checks
RUN apk add --no-cache \
    ca-certificates \
    tini \
    curl \
    jq \
 && rm -rf /var/cache/apk/*

# Create non-root user for security (matches Render UID/GID requirements)
RUN addgroup -S -g 1001 axelr \
 && adduser -S -u 1001 -G axelr -h /app -s /sbin/nologin axelr

# Create required directories with proper permissions
RUN mkdir -p /app/pb_data /etc/litestream /usr/local/bin \
 && chown -R axelr:axelr /app /etc/litestream

# Download and install PocketBase (static Go binary)
RUN wget -qO- https://github.com/pocketbase/pocketbase/releases/download/v${POCKETBASE_VERSION}/pocketbase_${POCKETBASE_VERSION}_linux_amd64.zip \
    | unzip -p - pocketbase > /usr/local/bin/pocketbase \
 && chmod +x /usr/local/bin/pocketbase

# Download and install Litestream (static Go binary)
RUN wget -qO- https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz \
    | tar -xzC /usr/local/bin litestream \
 && chmod +x /usr/local/bin/litestream

# Copy configuration files and startup script
COPY litestream.yml /etc/litestream.yml
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh && chown axelr:axelr /etc/litestream.yml /app/run.sh

# Switch to non-root user for runtime
USER axelr
WORKDIR /app

# Expose PocketBase port
EXPOSE 8090

# Healthcheck for Render platform to verify service liveness
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD curl -f http://localhost:8090/api/health || exit 1

# Entrypoint with tini to handle signal forwarding
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/app/run.sh"]