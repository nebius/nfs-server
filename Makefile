HELM_CHART_PATH ?= helm

HELM_UNITTEST_REPO    ?= oci://ghcr.io/helm-unittest/helm-unittest
HELM_UNITTEST_NAME    ?= unittest
HELM_UNITTEST_VERSION ?= 1.1.1

BOLD   := \033[1m
BLUE   := \033[34m
GREEN  := \033[32m
YELLOW := \033[33m
RESET  := \033[0m

.PHONY: lint
lint:
	@printf '$(BOLD)$(BLUE)==> Lint Helm chart$(RESET)\n'
	@helm lint $(HELM_CHART_PATH)
	@printf '$(BOLD)$(BLUE)<==$(RESET)\n\n'

.PHONY: _helmtest
_helmtest:
	@printf '$(BOLD)$(BLUE)==> Check Helm unittest plugin$(RESET)\n'
	@installed_version="$$(helm plugin list | awk -v name="$(HELM_UNITTEST_NAME)" 'NR > 1 && $$1 == name { print $$2; exit }')"; \
	need_install=0; \
	if [ -z "$$installed_version" ]; then \
		printf '$(YELLOW)Helm plugin $(HELM_UNITTEST_NAME) is missing$(RESET)\n'; \
		need_install=1; \
	elif [ "$$installed_version" != "$(HELM_UNITTEST_VERSION)" ]; then \
		printf '$(YELLOW)Helm plugin $(HELM_UNITTEST_NAME) version mismatch: found %s, expected $(HELM_UNITTEST_VERSION)$(RESET)\n' "$$installed_version"; \
		printf '$(YELLOW)Removing Helm plugin $(HELM_UNITTEST_NAME) %s$(RESET)\n' "$$installed_version"; \
		helm plugin uninstall $(HELM_UNITTEST_NAME); \
		need_install=1; \
	else \
		printf '$(GREEN)Helm plugin $(HELM_UNITTEST_NAME) $(HELM_UNITTEST_VERSION) is already installed$(RESET)\n'; \
	fi; \
	if [ "$$need_install" = "1" ]; then \
		printf '$(BLUE)Installing Helm plugin $(HELM_UNITTEST_NAME) $(HELM_UNITTEST_VERSION)$(RESET)\n'; \
		helm plugin install $(HELM_UNITTEST_REPO)/$(HELM_UNITTEST_NAME):$(HELM_UNITTEST_VERSION) --verify=false; \
	else \
		printf '$(GREEN)Skipping Helm plugin install$(RESET)\n'; \
	fi
	@printf '$(BOLD)$(BLUE)<==$(RESET)\n\n'

.PHONY: dep
dep:
	@printf '$(BOLD)$(BLUE)==> Building Helm chart dependencies$(RESET)\n'
	@helm dependency update $(HELM_CHART_PATH)
	@helm dependency build $(HELM_CHART_PATH)
	@printf '$(BOLD)$(BLUE)<==$(RESET)\n\n'

.PHONY: test
test: lint dep _helmtest
	@printf '$(BOLD)$(BLUE)==> Run Helm unit tests$(RESET)\n'
	@helm unittest $(HELM_CHART_PATH)
	@printf '$(BOLD)$(BLUE)<==$(RESET)\n\n'

.PHONY : all
all : test

.DEFAULT_GOAL := all
