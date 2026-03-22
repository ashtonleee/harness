FROM mitmproxy/mitmproxy:latest

COPY trusted/proxy/start_proxy.sh /opt/proxy/start_proxy.sh

RUN chmod +x /opt/proxy/start_proxy.sh

CMD ["/opt/proxy/start_proxy.sh"]
