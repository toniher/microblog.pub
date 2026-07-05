SHELL := /bin/bash
PWD=$(shell pwd)

.PHONY: build
build:
	docker build -t microblogpub-server .

.PHONY: config
config:
	# Run and remove instantly
	# The microblogpub_static volume is shared so the Twemoji emoji downloaded by the wizard persist and are served by the running container
	-docker run --rm -it --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv configuration-wizard

.PHONY: update
update:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv update --no-update-deps

.PHONY: prune-old-data
prune-old-data:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv prune-old-data

.PHONY: webfinger
webfinger:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv webfinger $(account)

.PHONY: move-to
move-to:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv move-to $(account)

.PHONY: self-destruct
self-destruct:
	-docker run --rm --it --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv self-destruct

.PHONY: reset-password
reset-password:
	-docker run --rm -it --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv reset-password

.PHONY: check-config
check-config:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv check-config

.PHONY: compile-scss
compile-scss:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv compile-scss

.PHONY: import-mastodon-following-accounts 
import-mastodon-following-accounts:
	-docker run --rm --volume `pwd`/data:/app/data --volume microblogpub_static:/app/app/static microblogpub-server inv import-mastodon-following-accounts $(path)
