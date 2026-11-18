# PATH 上的 python3 可能是缺标准库的托管运行时，显式给默认值
PYTHON ?= /usr/bin/python3

# 界面测试与截图都用离屏后端，没有显示器也能跑
QT_ENV := QT_QPA_PLATFORM=offscreen

.PHONY: all test check gui shot cli

all: test

test:
	@cd tests && $(QT_ENV) $(PYTHON) -m unittest discover -s . -p "test_*.py" -v

# 只报结果，不看逐条
check:
	@cd tests && $(QT_ENV) $(PYTHON) -m unittest discover -s . -p "test_*.py"

gui:
	@$(PYTHON) studio.py

# 离屏渲染界面截图到 docs/screenshot.png
shot:
	@PYTHONPATH=tests $(QT_ENV) $(PYTHON) tools/make_screenshot.py

cli:
	@$(PYTHON) cli.py --help
