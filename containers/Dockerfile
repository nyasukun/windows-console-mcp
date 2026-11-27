FROM ubuntu:26.04 AS common
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY containers/relay.py /opt/console/relay.py

FROM common AS linux
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    openssh-server iproute2 curl dnsutils procps nano \
    && mkdir -p /run/sshd && rm -f /etc/ssh/ssh_host_* && rm -rf /var/lib/apt/lists/*
ENV TERM=xterm-256color
CMD ["python3", "/opt/console/relay.py", "22", "/usr/sbin/sshd", "-D", "-e", "-f", "/endpoint/ssh/sshd_config"]

FROM common AS browser
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl \
    && curl -fSL --retry 3 https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends /tmp/chrome.deb fonts-noto-cjk \
    && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*
ENV HOME=/endpoint/home
CMD ["python3", "/opt/console/relay.py", "9222", "google-chrome-stable", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking", "--disable-sync", "--no-first-run", "--no-default-browser-check", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222", "--user-data-dir=/endpoint/home/chrome", "--window-size=1365,900", "about:blank"]
