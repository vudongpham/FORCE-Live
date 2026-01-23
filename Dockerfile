FROM python:3.10

WORKDIR /app

COPY . /app

RUN python -m pip install --no-cache-dir .

CMD []