# Repo-standard reproducible build for the BayWatch UI.
# (The live image was pushed with `ko`; this Dockerfile is the docker-equivalent.)
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /bw-ui .

FROM gcr.io/distroless/static:nonroot
COPY --from=build /bw-ui /bw-ui
EXPOSE 8080
USER 65532:65532
ENTRYPOINT ["/bw-ui"]
