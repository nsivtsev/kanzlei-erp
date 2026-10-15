# Local/CI only. Build the archived community server and admin client from immutable source.
FROM docker.io/library/golang:1.24.8-bookworm@sha256:4ed690d6649d63c312b99a6120025ec79ce3b542968a37da53d6236c7c61a848 AS build
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
ARG MC_COMMIT=7394ce0dd2a80935aded936b09fa12cbb3cb8096
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN git init /src/minio && cd /src/minio \
    && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth=1 origin "$MINIO_COMMIT" && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$MINIO_COMMIT" \
    && go build -trimpath -buildvcs=false -o /out/minio .
RUN git init /src/mc && cd /src/mc \
    && git remote add origin https://github.com/minio/mc.git \
    && git fetch --depth=1 origin "$MC_COMMIT" && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$MC_COMMIT" \
    && go build -trimpath -buildvcs=false -o /out/mc .

FROM docker.io/library/alpine:3.22.2@sha256:4b7ce07002c69e8f3d704a9c5d6fd3053be500b7f1c69fc0d80990c2ad8dd412
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
ARG MC_COMMIT=7394ce0dd2a80935aded936b09fa12cbb3cb8096
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
    org.opencontainers.image.revision="$MINIO_COMMIT" \
    org.kanzlei.minio.mc-revision="$MC_COMMIT"
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/minio /out/mc /usr/local/bin/
RUN addgroup -g 10001 minio && adduser -D -u 10001 -G minio minio \
    && mkdir /data && chown minio:minio /data
USER 10001:10001
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
