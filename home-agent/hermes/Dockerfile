FROM nousresearch/hermes-agent:latest@sha256:d4da4a40cd7a28aba983775d9fd31d94cbf153eeb0cb9e844d6d0f612b7c24db

USER root

# The base image deliberately omits packet-filtering tools. They are used only
# by the entrypoint below, before Hermes and Chromium start.
RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint-network-guard.sh /usr/local/bin/hermes-network-guard
RUN chmod 0755 /usr/local/bin/hermes-network-guard

ENTRYPOINT ["/usr/local/bin/hermes-network-guard"]
