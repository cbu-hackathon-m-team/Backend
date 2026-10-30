FROM python:3.12-slim
WORKDIR /app
COPY src/ossguard/mock_service.py /app/mock_service.py
COPY src/ossguard/relay_service.py /app/relay_service.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "-u", "mock_service.py"]
