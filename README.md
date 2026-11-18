# can-studio

CAN / UDS 诊断上位机。面向 [can-node](https://github.com/Dik-910/can-node) 的
诊断服务与 Bootloader，也适用于任何遵守 ISO 14229 的 ECU。

分为三层，界面只依赖最上面那层：

```
link.py    链路：把「发一帧、等一帧」抽象出来（SocketCAN / 假链路）
uds.py     协议：ISO 14229 请求编码、响应解码、否定响应与 0x78 处理
dtc.py     语义：故障码、状态位、快照的解读
gui/       Qt 界面（待接入），不碰协议细节
```

这样分层的原因：协议层要能脱离硬件测试。`tests/` 里用假链路把整套服务跑一遍，
否定响应、0x78 挂起、超时、回显不匹配这些情况都能精确构造出来，不必在真实
总线上撞运气。

## 当前状态

| 模块 | 状态 |
|---|---|
| `canstudio/link.py` | 已实现：SocketCAN + ISO-TP、RecordingLink（记录收发） |
| `canstudio/uds.py` | 已实现：`0x10/11/14/19/22/27/2E/31/34/36/37/3E` |
| `canstudio/dtc.py` | 已实现：DTC 编解码、状态位、快照解析（0x19 六个子功能） |
| `canstudio/dids.py` | 已实现：DID 表与解码，未知 DID 按原始字节展示 |
| `canstudio/flash.py` | 已实现：HEX/S-record 解析、分块、序号回绕、重试、CRC 交叉校验 |
| `cli.py` | 已实现：会话、DID 读写、故障码、刷写、原始请求、监视 |
| `gui/` | 未开始（PySide6） |

测试 69 项，全部不依赖 CAN 接口。

## 用法

```bash
./cli.py did read VIN 0x0101 0x0302
./cli.py did write 0x0201 3C
./cli.py dtc list
./cli.py dtc snapshot --record 1
./cli.py dtc clear
./cli.py flash build/app.hex
./cli.py raw 22 F1 90
./cli.py monitor
```

默认对准 `vcan0`、请求 `0x7E0`、响应 `0x7E8`，与 can-node 的 `ecu-server` 一致。
换 ECU 用 `--if / --tx / --rx`，换密钥算法用 `--key-file`（文件里定义
`key_algo(level, seed, params)`）。

```
$ ./cli.py did read VIN 0x0101 0x0302
0xF190  VIN          LSVAA1234567890AB
0x0101  电池电压      3660 mV (3.66 V)
0x0302  故障存储概览   操作循环 1，故障条目 0
```

`0x0201` 是可写的过压阈值，写入要先切扩展会话再解锁，这一条同时验证了
会话控制、安全访问与写服务：

```
$ ./cli.py did write 0x0201 3C
0x0201 过压阈值 写入成功（1 字节）
$ ./cli.py did read 0x0201
0x0201  过压阈值       60
```

## 实测

对接 `can-node` 的 `ecu-server`（vcan0，请求 0x7E0 / 响应 0x7E8）：

| 命令 | 结果 |
|---|---|
| `did read VIN 0x0101 0x0302` | VIN 17 字节走多帧读回、电压解出 3.66 V、故障概览解出循环与条目数 |
| `did write 0x0201 3C` → `did read 0x0201` | 写入 60 并读回一致（需扩展会话 + 安全访问） |
| `dtc list` | 无故障 |
| `dtc supported` | `U010087 0xC10087` |
| `raw 22 F1 90` | 原始响应 `62 F1 90 4C 53 56 41 ...` |
| `flash fw.hex -y` | 2048 字节 / 9 块 / 0.05 s，ECU 侧算出的 CRC32 与本机一致 |

刷写的输出：

```
固件 fw.hex: 0x08000000..0x08000800  2048 字节  crc32 0xB9D45861
  [#########################] 100%  2048/2048
完成：2048 字节 / 9 块，0.05 s
CRC 校验通过：0xB9D45861
```

## 协议细节

几处实现上容易错、且已经写进代码注释的地方：

**`0x34` 的两个 ALFID 不是一回事。** 请求里 `ALFID = 0x44`，高半字节是地址
字节数、低半字节是长度字节数；响应里高半字节才是 `maxNumberOfBlockLength` 的
字节数。两个方向混用，块大小会算成 0 或者超长。另外这个长度包含 SID 与块序号
两字节，真正能装的数据要减 2。

**`0x78` 不是失败。** ECU 回 `7F xx 78` 表示「在处理」，此时要按 P2\* 重新计时
继续等，既不能重发请求，也不能判超时。

**DTC 文本固定 6 位十六进制。** `0xC10087` 显示为 `U010087`，不省前导零：
`U10087` 这种 5 位写法与 2 字节 DTC 的 4 位写法混在一起就分不清了。解析时
两种写法都接受，现场手抄的码常漏零。

## 测试

```bash
make test      # 54 项，verbosity=2
make check     # 只报结果
```

用例覆盖的是请求编码与响应解码本身：`0x10 03` 是否原样发出、`0x27` 的种子
如何算成密钥、`74 20 02 00` 是否解析出 512 字节的块长、0x19 各子功能的响应
是否按标准切开。

## 对接 ECU 的前提

- CAN 接口就绪：`sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0`
- ECU 侧在跑：`cd ../can-node && ./ecu-server --if vcan0`
- ID 与 can-node 一致：请求 `0x7E0`、响应 `0x7E8`

DID 表与密钥算法都按 can-node 的 `ecu-server` 定义（`canstudio/dids.py`
与 `uds.default_key_algo`）。换 ECU 时改这两处，协议层不用动。

## 相关仓库

| 仓库 | 关系 |
|---|---|
| [can-node](https://github.com/Dik-910/can-node) | 被诊断的 ECU 节点固件，本工具的对接目标 |
| [can-gateway](https://github.com/Dik-910/can-gateway) | 车载 CAN 网关，可在本工具与 ECU 之间转发 |
| [bms-node](https://github.com/Dik-910/bms-node) | 储能 BMS 节点，复用同一套 UDS 服务端 |
| [mcu-gateway](https://github.com/Dik-910/mcu-gateway) | Modbus/RS485 网关，另一条链路 |

## 许可证

MIT

## 参考的同类项目

协议与界面都不必从零设计，几个公开项目的做法有直接借鉴价值：

| 项目 | 借鉴的点 |
|---|---|
| [UDT](https://github.com/hmf1235789/UDT) | 按 `core / transports / services / gui` 分包，服务用注册表扩展 |
| [UDS_Python_Prj](https://github.com/teddy1122/UDS_Python_Prj) | 刷写上位机的完整流程：固件文件解析 → ISO-TP → UDS 传输服务 |
| [uds_tracer](https://github.com/aslansq/uds_tracer) | 报文监视界面的信息组织（时间、方向、原始帧、解析结果） |
| [python-can](https://github.com/hardbyte/python-can) | `can.viewer` 的后台收帧方式与界面骨架 |
| [cantools](https://github.com/cantools/cantools) | DBC 解码，监视页按信号名展示而非裸字节 |

本仓库的取舍：协议层自己实现（这样两侧实现独立，互操作测试才有意义），
界面层用 PySide6（官方绑定，LGPL，与 MIT 兼容；PyQt5 是 GPL，不能与 MIT 混用），
固件文件解析用 `bincopy`（Intel HEX 与 S-record 的格式细节不必自己维护）。
