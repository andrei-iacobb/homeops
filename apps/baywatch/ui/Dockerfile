# Repo-standard reproducible build for the BayWatch UI.
# (The live image was pushed with `ko`; this Dockerfile is the docker-equivalent.)
FROM golang:1.27.3-alpine@sha256:d200d2dd76b03a99b52651fd1325aff7dcd35422c35c01bac56571b0507f3deb AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /bw-ui .

FROM gcr.io/distroless/static:nonroot@sha256:18f4cc26c9ffa7f20a359298be7deadac5c607c8e03a02b8119f1a5fc2f82c99
COPY --from=build /bw-ui /bw-ui
EXPOSE 8080
USER 65532:65532
ENTRYPOINT ["/bw-ui"]
