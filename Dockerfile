# Official AgentHack base image. Do not replace it with a generic Python base:
# the platform expects the SDK-owned /health and /solve endpoints and telemetry.
FROM agenthack/base:1.1.1

WORKDIR /app

ARG TEAM_ID=team-001
ARG AGENT_VERSION=1.0.0
LABEL org.agenthack.team_id="${TEAM_ID}"
LABEL org.agenthack.version="${AGENT_VERSION}"

# Extra dependencies must be installed at build time; the evaluation container
# has no network access. The base image has fastapi/openai/httpx/pydantic; pypdf
# is the only extra runtime dependency, so it is vendored as pure Python to make
# the build reproducible without PyPI access.
USER root
# Remove the sample agent shipped in the base image; the contest entrypoint is agent.py.
RUN rm -rf /app/src
# The platform mounts the writable result workspace at /output.
RUN mkdir -p /output && chown agent:agent /output
ENV CONTEST_WORKSPACE_ROOT=/output
COPY --chown=agent:agent vendor/python/pypdf /usr/local/lib/python3.11/site-packages/pypdf
COPY --chown=agent:agent vendor/python/pypdf-5.9.0.dist-info /usr/local/lib/python3.11/site-packages/pypdf-5.9.0.dist-info

COPY --chown=agent:agent agent.py agenthack_compat.py tools.py /app/
COPY --chown=agent:agent core /app/core
COPY --chown=agent:agent handlers /app/handlers
COPY --chown=agent:agent d_adapter /app/d_adapter

# Public sample requests are teaching material (no answers). Bake them in so
# the self-test/repair loop works even if the platform mounts only case dirs.
COPY --chown=agent:agent vendor/d-class/公开样例请求 /app/d-class-samples/公开样例请求

# The contest guide asks for the team's gateway URL/key/model to be baked into
# the image. Runtime env vars take precedence in core/config.py. Create this
# file from agent_config.example.json before building; never commit it.
COPY --chown=agent:agent agent_config.json /app/agent_config.json

USER agent
EXPOSE 8080
CMD ["python", "agent.py"]
