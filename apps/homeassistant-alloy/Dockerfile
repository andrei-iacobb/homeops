FROM docker.io/grafana/alloy:v1.21.3@sha256:8ab871206bd2d8f89277f53abffee5f452a293980788d52f85fa183a87a505a4
COPY config.alloy /etc/alloy/config.alloy
ENTRYPOINT ["/bin/alloy"]
CMD ["run", "--storage.path=/data/alloy", "--server.http.listen-addr=127.0.0.1:12345", "/etc/alloy/config.alloy"]
