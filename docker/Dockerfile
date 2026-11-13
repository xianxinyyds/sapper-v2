# sapper agent image for CTFY-0001 v3 single-challenge instance runs.
#
# Build:  docker build -f docker/Dockerfile -t sapper-ctfy:latest .
# Export: bash docker/build.sh          ->  dist/sapper-agent.tar.gz
FROM kalilinux/kali-rolling:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/home/agent/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    SAPPER_RETRY_MODEL=3

# --- offline tooling (Kali packages) -----------------------------------------
# Pin a reliable mirror: the default http.kali.org redirect can land on
# mirrors that fail to resolve in build sandboxes.
RUN echo 'deb http://mirrors.tuna.tsinghua.edu.cn/kali kali-rolling main contrib non-free non-free-firmware' \
        > /etc/apt/sources.list \
    && echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80-retries
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget git jq unzip zip file xxd less vim \
        procps psmisc lsof tcpdump tshark ngrep iproute2 iputils-ping net-tools \
        python3 python3-pip \
        nodejs npm \
        ruby ruby-dev \
        netcat-traditional socat \
        dnsutils whois openssh-client sshpass \
        openssl gnupg \
        nmap masscan \
        whatweb wafw00f nikto \
        gobuster ffuf dirb wfuzz feroxbuster dirsearch arjun cewl \
        sqlmap \
        hydra \
        binwalk steghide exiftool stegseek pngcheck \
        ffmpeg sox libsox-fmt-all \
        poppler-utils zbar-tools \
        7zip fcrackzip john \
        upx-ucl radare2 yara \
        libgmp-dev libmpfr-dev libmpc-dev \
        python3-fpylll python3-ecdsa hashid hash-identifier \
        python3-numpy python3-onnx python3-safetensors \
        redis-tools default-mysql-client postgresql-client \
        gdb gdbserver checksec strace ltrace patchelf gcc make libc6-dev ropper \
        smbclient smbmap \
        ldap-utils \
        enum4linux \
        responder impacket-scripts \
        proxychains4 \
        seclists \
        sudo \
    && setcap -r /usr/lib/nmap/nmap \
    && rm -rf /var/lib/apt/lists/* \
    && du -sh /usr/share/seclists \
    && rm -rf /usr/share/seclists/IOCs \
              /usr/share/seclists/Misc /usr/share/seclists/Username_Lists \
              /usr/share/seclists/Discovery/DNS \
    && find /usr/share/doc -mindepth 1 -maxdepth 1 \
           ! -name python3-impacket -exec rm -rf {} + \
    && rm -rf /usr/share/man /usr/share/vim/vim*/doc \
    && du -sh /usr/share/seclists

RUN gem install --no-document zsteg --version 0.2.14 --bindir /usr/local/bin \
    && gem install --no-document one_gadget --version 2.1.1 \
    && gem install --no-document seccomp-tools --version 1.7.1 \
    && zsteg --help >/dev/null \
    && one_gadget --help >/dev/null \
    && seccomp-tools --help >/dev/null

# nuclei engine only; the full template repository is intentionally not bundled.
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
        requests pyjwt "unicorn==2.0.1.post1" "capstone>=5.0.1,<6" "setuptools<81" pwntools \
        pycryptodome sympy ROPgadget \
        pycryptodomex ratelimit termcolor cprint \
        "git-dumper==1.0.9" "xortool==1.1.0"
# heavy/compiled extras: failure here must not kill the build (kb mentions them
# as optional; stdlib+sympy covers most crypto)
RUN pip3 install --no-cache-dir --break-system-packages gmpy2 z3-solver \
        || echo "WARN: gmpy2/z3 unavailable — continuing without them"

COPY docker/pwninit /usr/local/bin/pwninit
COPY docker/RsaCtfTool.tar.gz /tmp/RsaCtfTool.tar.gz
COPY docker/jwt_tool.tar.gz /tmp/jwt_tool.tar.gz
RUN chmod +x /usr/local/bin/pwninit \
    && mkdir -p /tmp/RsaCtfTool \
    && tar -xzf /tmp/RsaCtfTool.tar.gz --exclude='*/.venv' --strip-components=1 -C /tmp/RsaCtfTool \
    && pip3 install --no-cache-dir --break-system-packages /tmp/RsaCtfTool \
    && mkdir -p /opt/jwt_tool \
    && tar -xzf /tmp/jwt_tool.tar.gz --strip-components=1 -C /opt/jwt_tool \
    && chmod +x /opt/jwt_tool/jwt_tool.py \
    && python3 -c "from pathlib import Path; p=Path('/opt/jwt_tool/jwt_tool.py'); s=p.read_text(); old='configparser.ConfigParser(allow_no_value=True)'; assert s.count(old) == 1; p.write_text(s.replace(old, 'configparser.ConfigParser(allow_no_value=True, delimiters=(\"=\",))'))" \
    && ln -s /opt/jwt_tool/jwt_tool.py /usr/local/bin/jwt_tool \
    && rm -rf /tmp/RsaCtfTool \
    && rm -f /tmp/RsaCtfTool.tar.gz /tmp/jwt_tool.tar.gz \
    && pwninit --help >/dev/null \
    && RsaCtfTool --help >/dev/null \
    && jwt_tool --help >/dev/null

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

RUN runuser -u agent -- jwt_tool config-init || test -s /home/agent/.jwt_tool/jwtconf.ini

COPY docker/entrypoint-ctfy.sh /usr/local/bin/sapper-entrypoint
RUN chmod +x /usr/local/bin/sapper-entrypoint

USER agent
WORKDIR /ctfy/work
ENTRYPOINT ["/usr/local/bin/sapper-entrypoint"]
