# dca —— 開發環境代管（agent → controller → devbox）

三個角色，全部用人類可讀的名字，不用 A／B／C 代號：

```
agent        ──HTTP + Bearer token──▶  controller（唯一持 docker socket）
（你的 agent 容器，host 網路）            │ docker run（參數全部寫死在 app.py）
                                          ▼
                                        devbox（開發環境，cap_drop=ALL、
                                                no-new-privileges、無 socket、零 git 憑證）
```

| 名字 | 是甚麼 | 持有 | 執行方式 |
|---|---|---|---|
| **agent** | 你的 agent 容器，跑 coding task | `DCA_TOKEN`（`DCA_URL` 可不設） | 預設 bridge 即可，經 `172.17.0.1:47123` 呼叫 |
| **controller** | `devbox-controller` 容器，跑 `app.py` | docker socket、git 憑證 | `network_mode: host`，監聽 `0.0.0.0:47123` |
| **devbox** | `devbox-<task>` 容器，一個 task 一個 | 只有 `/workspace` | 由 controller 動態建立 |

**不需要 `docker network create`。** controller 用 `network_mode: host`，agent 留在 docker 默認 bridge，兩者經 docker0 網關（`172.17.0.1`）互通，認證只靠 Bearer token。

> 「哪個 IP」這個問題的答案：**agent 容器看宿主 = 它所在 bridge 的網關**，默認 bridge 就是 `172.17.0.1`（除非宿主 `daemon.json` 改過 `bip`）。`dc` 未設 `DCA_URL` 時會依序探測 `172.17.0.1` → `127.0.0.1`，兩個都是常數，不需要固定 subnet。

## 為什麼這樣切

| 問題 | 處理 |
|---|---|
| agent 拿到 docker socket 等於宿主 root | agent 永遠不掛 socket，只持 token |
| repo 的 `devcontainer.json` 可能是攻擊面 | 不讀它。環境 100% 由 owner 寫的模板決定 |
| agent 會加 `--privileged`／`--mount` | 它沒有入口：API 只收模板名 + task 名 + cmd 陣列 |
| git 憑證被 devbox 逃逸偷走 | clone／push 由 controller 在宿主執行，devbox 內零憑證 |
| devbox 反打 controller | devbox 雖在同一個 bridge，但沒有 token，API 一律 401；加上 controller 只操作帶 label 的容器 |
| 沒有 network 分段怎麼辦 | Bearer token（`hmac.compare_digest`）＋ 只操作帶 `devbox.owner` label 的容器 |
| 機器不能建 network | 全條路徑都不呼叫 `docker network create`；devbox 不傳 `--network` 就用內建 bridge |

## 部署（pi5）

```sh
# 1. 取你的 uid/gid 與 docker 組 gid
id -u; id -g; ls -l /var/run/docker.sock

# 2. 建工作目錄（controller 與 devbox 共用同一個宿主路徑）
mkdir -p /storage/dca/work /storage/dca  && cd /storage/dca

# 3. 解壓本包後
cp .env.example .env && vi .env      # 填 DCA_TOKEN / DCA_HOST_WORK_ROOT / DOCKER_GID

# 4. 起 controller
docker compose -f compose.yml up -d --build
docker compose -f compose.yml logs -f controller
```

啟動日誌應見：`dca controller listening on 0.0.0.0:47123 (owner=agent, work_root=..., network=(docker default))`。

若之前跑過舊版（有 `dca-abnet`／`dca-c` 網絡），先清掉：

```sh
docker compose -f compose.yml down
docker rm -f $(docker ps -aq --filter label=devbox.owner=agent) 2>/dev/null
docker network rm dca-abnet dca-c 2>/dev/null
```

## 自檢

controller 綁 loopback，從 agent 容器（host 網路）或臨時 host 網路容器測：

```sh
cd /storage/dca/dca && source .env
# 從任何 bridge 容器內：
docker run --rm -e T="$DCA_TOKEN" curlimages/curl:latest \
  -s -H "Authorization: Bearer $T" http://172.17.0.1:47123/v1/health

# 從宿主（host 網絡）：
curl -s -H "Authorization: Bearer $DCA_TOKEN" http://127.0.0.1:47123/v1/health
```

預期 200 加 JSON；去掉 header 應回 **401** —— 那才是 token 這層真的在運作。

## 在 agent 容器裡怎麼用

把 `client/dc` 放進 agent 的 PATH（需要 python3），設兩個環境變數：

```sh
export DCA_TOKEN=<與 .env 相同>
# DCA_URL 不用設：dc 會自己試 172.17.0.1 再試 127.0.0.1
# 若宿主改過 docker bip，才需要 export DCA_URL=http://<你的網關>:47123
```

```sh
dc templates                      # 看有哪些模板可點
dc new node22 task-001            # 起一個空的 devbox
dc exec task-001 -- git --version
dc clone task-001 https://github.com/you/repo.git
dc exec task-001 -- sh -lc 'cd repo && npm ci && npm test'
dc push task-001 repo
dc rm task-001
```

**`dc exec` 用 `sh -lc '...'` 包一層**：cmd 是字串陣列，不經宿主 shell。要讓 devbox 內 shell 解釋管道與 `&&`，就顯式叫 `sh -lc`。

## 模板怎麼寫

`templates/<name>.tpl`，每個欄位一行 `key: value`：

```yaml
name: node22
image: node:22-alpine@sha256:1314fb30...   # 必須帶 digest
workdir: /workspace
network: default      # default = 不傳 --network；none = 完全離網；其他 = 用已存在的網絡
cpus: 2
memory: 4g
pids: 512
user: "1000:1000"
```

- 只允許上述 8 個欄位（另有 `notes`），出現其他欄位（例如 `privileged`）整份拒絕。
- `image` 不帶 `@sha256:` 會被拒（除非 `DCA_ALLOW_TAGS=1`，不建議）。
- 模板目錄以 `:ro` 掛進 controller；agent 看不到它，自然改不了。

## egress 收窄（可選，未內建）

devbox 用 docker 內建 bridge，能出公網也能打 20.x 內網。要擋內網，在**宿主**加 nftables 規則（`apk add nftables`）：

```sh
nft add table inet dca
nft 'add chain inet dca fwd { type filter hook forward priority -10; policy accept; }'
nft add rule inet dca fwd iifname "docker0" ip daddr { 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12, 169.254.0.0/16, 127.0.0.0/8 } counter drop
nft add rule inet dca fwd iifname "docker0" ip6 daddr { fc00::/7, fe80::/10, ::1/128 } counter drop
```

devbox 仍需要出公網（clone、npm），所以只擋內網方向。若目標機連 nftables 都加不了，這一節做不到，見下面第 6 條。

## 運維

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `DCA_TOKEN` | 無（必填） | ≥16 字符，`openssl rand -hex 32` |
| `DCA_HOST_WORK_ROOT` | 無（必填） | 宿主路徑，controller 內以同一路徑掛載 |
| `DCA_BIND` | `0.0.0.0` | controller 綁定地址。要只給 host 網絡用就改 `127.0.0.1`，但 bridge 內的 agent 就連不上 |
| `DCA_PORT` | `47123` | controller 監聽 port |
| `DCA_NETWORK` | 空 | devbox 預設網絡；空或 `default` = 不傳 `--network` |
| `DCA_OWNER` | `agent` | label，只操作此 label 的容器 |
| `DCA_EXEC_TIMEOUT` | `900` | 單次 exec 上限（秒） |
| `DCA_IDLE_HOURS` | `0` | >0 則定時 stop 閒置 task |
| `DCA_ALLOW_TAGS` | `0` | 是否允許 image tag（不建議開） |
| `DCA_ALLOW_PRIVATE_URL` | `0` | 是否允許 clone 內網 URL（不建議開） |

## 已實測（本機，非 pi5）

- 32 項單元測試全通過（模板越界、tag、`privileged` 欄位、路徑 `..`、URL 內網、task 注入、exec 字串注入）
- 真實 HTTP：無 token 的 health 回 401；帶 token 的 templates／create 回 200／201
- 實際送 docker 的 `run` 參數逐字檢視：無特權、無 socket、resources 齊全、預設不帶 `--network`
- `py_compile` 通過

未驗證：controller image 在 pi5 上 build、`group_add` 拿到 socket、devbox 實際起得來、arm64 digest 拉得到。

## 已知邊界（誠實清單）

1. **devbox 逃逸仍是唯一大風險**：共享內核，需要真的 kernel／runc CVE；一旦成功就是 pi5 root（dockerd 是 root）。要收窄只能換 rootless dockerd。
2. **controller 是新的信任根**：它被攻破等於宿主 root。它的 image 只有 python3 + docker-cli + git。
3. **repo 的程式碼仍會被執行**（`npm ci`、`make`、`pytest`）：那是功能本身；影響範圍先在 devbox 內。
4. **token 是唯一認證**（沒有 network 分段、controller 綁 `0.0.0.0`）：
   - token 洩漏 = 可以建 devbox；
   - `0.0.0.0` 代表 20.x 內網與 docker bridge 都能連到 47123，全靠 token 擋：token 要 32 bytes 以上、定期輪換；
   - devbox 與 agent 同處默認 bridge，devbox 內的程式可以碰 agent 容器並嘗試偷 token —— 這是放棄 network 分段的直接代價，比 host 網絡方案差一級。
5. **agent 若也能直接碰宿主 shell，整套失效**：agent 必須真的只有 HTTP 一條路。
6. **無 network 分段 = 少了第二道牆**：token 洩了、或 controller 有 RCE，就沒有網絡層可以拖時間。這是「目標機不能建 network」換來的代價。
7. **`172.17.0.1` 是默認 bridge 的網關**：若宿主 `daemon.json` 設過 `bip`／`default-address-pools`，或容器被接去自訂網絡，這個 IP 就不適用，要自行設 `DCA_URL`。
8. **資源與回收未強制**：`DCA_IDLE_HOURS` 預設 0（不回收），長駐 devbox 會累積。
