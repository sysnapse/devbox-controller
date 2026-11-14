# base —— 最小 Alpine，用于只读分析／脚本（L0）
name: base
image: alpine:3.21@sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab
workdir: /workspace
network: default
cpus: 1
memory: 1g
pids: 256
user: "1000:1000"
