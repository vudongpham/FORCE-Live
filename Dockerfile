FROM python:3.10

WORKDIR /app

# print log lines immediately (stdout is not a terminal in docker without -t)
ENV PYTHONUNBUFFERED=1

COPY . /app

RUN python -m pip install --no-cache-dir .

EXPOSE 2741

CMD []
