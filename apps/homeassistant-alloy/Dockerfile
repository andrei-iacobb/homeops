FROM docker.io/grafana/alloy:v1.22.0@sha256:9acc2529bc1edb7f5b2bd801dd8334ebc80c158e3a9e5526ecd7c9a273d43189
COPY config.alloy /etc/alloy/config.alloy
ENTRYPOINT ["/bin/alloy"]
CMD ["run", "--storage.path=/data/alloy", "--server.http.listen-addr=127.0.0.1:12345", "/etc/alloy/config.alloy"]
