FROM python:3.12-slim
WORKDIR /app
COPY namegen ./namegen
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python", "-m", "namegen.server"]
