# node22 —— Node.js 22 开发环境（L1）
# image 必须带 @sha256 digest，禁止 tag
name: node22
image: node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a
workdir: /workspace
network: default
cpus: 2
memory: 4g
pids: 512
user: "1000:1000"
