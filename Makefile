# PATH 上的 python3 可能是缺标准库的托管运行时，显式给默认值
PYTHON ?= /usr/bin/python3

.PHONY: all test check cli

all: test

test:
	@cd tests && $(PYTHON) -m unittest discover -s . -p "test_*.py" -v

# 只报结果，不看逐条
check:
	@cd tests && $(PYTHON) -m unittest discover -s . -p "test_*.py"

cli:
	@$(PYTHON) cli.py --help
