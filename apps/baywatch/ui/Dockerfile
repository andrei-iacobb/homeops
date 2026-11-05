# Repo-standard reproducible build for the BayWatch UI.
# (The live image was pushed with `ko`; this Dockerfile is the docker-equivalent.)
FROM golang:1.27.3-alpine@sha256:fa4881266890ef051f0ce63afdfaccb69bbe8186084387f4fafd60d7ecbabc71 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /bw-ui .

FROM gcr.io/distroless/static:nonroot@sha256:25ac8f0c3b2334a9eb05452266bd4697864a8c8ef0ef71e5e6480d7f92868329
COPY --from=build /bw-ui /bw-ui
EXPOSE 8080
USER 65532:65532
ENTRYPOINT ["/bw-ui"]
