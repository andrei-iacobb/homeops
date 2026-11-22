# Repo-standard reproducible build for the BayWatch UI.
# (The live image was pushed with `ko`; this Dockerfile is the docker-equivalent.)
FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /bw-ui .

FROM gcr.io/distroless/static:nonroot@sha256:8ce6dd87e9823c5b48dcfb2c8604714ccb351ce59607327bcc4957448da279b5
COPY --from=build /bw-ui /bw-ui
EXPOSE 8080
USER 65532:65532
ENTRYPOINT ["/bw-ui"]
