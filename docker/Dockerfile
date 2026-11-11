# sapper agent image for CTFY-0001 v3 single-challenge instance runs.
#
# Build:  docker build -f docker/Dockerfile -t sapper-ctfy:latest .
# Export: bash docker/build.sh          ->  dist/sapper-agent.tar.gz
FROM kalilinux/kali-rolling:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/home/agent/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    SAPPER_RETRY_MODEL=3

# --- offensive tooling (kali repo, curated to stay < 3GB) -------------------
# Pin a reliable mirror: the default http.kali.org redirect can land on
# mirrors that fail to resolve in build sandboxes.
RUN echo 'deb http://mirrors.tuna.tsinghua.edu.cn/kali kali-rolling main contrib non-free non-free-firmware' \
        > /etc/apt/sources.list \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq unzip zip file xxd less vim \
        procps psmisc lsof tcpdump iproute2 iputils-ping net-tools \
        python3 python3-pip \
        nodejs npm \
        netcat-traditional socat \
        dnsutils whois \
        openssl \
        nmap masscan \
        whatweb wafw00f nikto \
        gobuster ffuf dirb wfuzz \
        sqlmap \
        hydra \
        binwalk steghide exiftool \
        libgmp-dev libmpfr-dev libmpc-dev \
        redis-tools default-mysql-client postgresql-client \
        gdb checksec \
        smbclient smbmap \
        ldap-utils \
        enum4linux \
        responder impacket-scripts \
        proxychains4 \
        seclists \
        sudo \
    && rm -rf /var/lib/apt/lists/* \
    && du -sh /usr/share/seclists \
    && rm -rf /usr/share/seclists/IOCs \
              /usr/share/seclists/Misc /usr/share/seclists/Username_Lists \
              /usr/share/seclists/Discovery/DNS \
    && rm -rf /usr/share/doc /usr/share/man /usr/share/vim/vim*/doc \
    && du -sh /usr/share/seclists

# nuclei (Go release) — CVE-template runner referenced by the kb.
# The zip is downloaded on the host (proxy) and shipped via build context:
# the build sandbox cannot reach github.
COPY docker/nuclei.zip /tmp/nuclei.zip
RUN unzip -o /tmp/nuclei.zip -d /usr/local/bin nuclei \
    && chmod +x /usr/local/bin/nuclei && rm -f /tmp/nuclei.zip

# --- model CLI --------------------------------------------------------------
# --allow-scripts: kali's npm blocks postinstall by default, but claude-code
# ships a native binary that its postinstall (node install.cjs) must fetch.
RUN npm install -g --allow-scripts=@anthropic-ai/claude-code \
        @anthropic-ai/claude-code @openai/codex@0.167.0 \
    && claude --version \
    && codex --version \
    && npm cache clean --force

# --- python extras ----------------------------------------------------------
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip3 install --no-cache-dir --break-system-packages \
        requests pyjwt "unicorn==2.0.1.post1" pwntools \
        pycryptodome sympy ROPgadget
# ropper's filebytes dependency still uses ast.Str, removed in Python 3.14.
RUN pip3 install --no-cache-dir --break-system-packages ropper \
        || echo "WARN: ropper unavailable — continuing without it"
# heavy/compiled extras: failure here must not kill the build (kb mentions them
# as optional; stdlib+sympy covers most crypto)
RUN pip3 install --no-cache-dir --break-system-packages gmpy2 z3-solver \
        || echo "WARN: gmpy2/z3 unavailable — continuing without them"

# --- sapper -----------------------------------------------------------------
COPY lib/        /opt/sapper/lib/
COPY templates/  /opt/sapper/templates/
COPY kb/         /opt/sapper/kb/
COPY README.md LICENSE SKILL.md /opt/sapper/

# --- non-root agent user ----------------------------------------------------
# Claude Code refuses --dangerously-skip-permissions as root; run unprivileged
# with a writable HOME for the model CLI's state.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.local/bin /ctfy/work \
    && ln -sf /opt/sapper/lib/sapper.py /home/agent/.local/bin/sapper \
    && chmod +x /opt/sapper/lib/sapper.py \
        /opt/sapper/lib/ctfy_run.py /opt/sapper/lib/codex_engine.py \
    && chown -R agent:agent /ctfy/work /opt/sapper /home/agent

COPY docker/entrypoint-ctfy.sh /usr/local/bin/sapper-entrypoint
RUN chmod +x /usr/local/bin/sapper-entrypoint

USER agent
WORKDIR /ctfy/work
ENTRYPOINT ["/usr/local/bin/sapper-entrypoint"]
