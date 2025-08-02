-include .env
export
DB_IMAGE = openrecall-db
DB_CONTAINER = openrecall-db
DB_PORT = 5432

docker-build:
	docker build -t $(DB_IMAGE) -f Dockerfile.postgres .

docker-start:
	docker run -d --name $(DB_CONTAINER) -e POSTGRES_PASSWORD=$(DB_PASSWORD) -e POSTGRES_DB=$(DB_NAME) -p $(DB_PORT):5432 $(DB_IMAGE)

docker-stop:
	docker stop $(DB_CONTAINER)
	docker rm $(DB_CONTAINER) 