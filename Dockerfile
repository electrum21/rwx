# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# src.tar.gz: the .py files of sqlglot and its tests at the pinned commit
# eb11aec4be8cb2122afe713a4816662c3f0fb2fa, so the selector can apply each
# patch and compare syntax trees. maps.json.gz: per-test coverage of that commit.
COPY selector.py maps.json.gz src.tar.gz /app/
RUN mkdir /app/src && tar -xzf /app/src.tar.gz -C /app/src && rm /app/src.tar.gz \
    && chmod -R a+rX /app \
    && python -c "import compileall,sys; sys.exit(not compileall.compile_file('/app/selector.py', quiet=1))"
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
