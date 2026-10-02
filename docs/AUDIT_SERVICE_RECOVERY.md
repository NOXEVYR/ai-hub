# 服务启动与异常恢复约定

`python server.py serve` 与桌面启动的服务都向 `data/service-runtime.log` 追加运行事件：启动 PID、端口、实例 ID，HTTP 方法与状态，以及异常类型和代码位置、正常停机。日志达到 2 MiB 时轮转，最多保留 3 份历史；请求 URL、查询串、正文、鉴权头及任意异常值不进入该日志。桌面入口原有 `data/server.log`（启动输出）和 `data/launcher.log`（启动器错误）继续保留，不被运行日志覆盖。启动守卫拒绝的安全恢复提示也会写入运行日志。

`data/server.pid.json` 由服务自身在发布控制身份后原子写入，含 PID、端口、安装根、instance_id、来源和运行日志位置；启动器不再写第二份身份。正常退出只清理实例 ID 相同的 PID 快照和控制文件。强制结束可能留下快照，因此 **PID 文件存在不代表服务存活**。排障应使用 `data/desktop/server-control.json` 的实例身份与本安装的 `/api/health` 响应核对；不要输出控制文件中的 token，也不要仅凭 PID 结束进程。

登记主文件与备份双损坏、尚未配置资产根目录等预期读取错误通过登记 GET 返回可读 4xx，不返回 Python traceback。单损坏仍只读有效备份，所有情况都不自动覆盖损坏文件。

超出 2 MiB 的请求体返回 413 JSON，错误码 `request_too_large`。为使普通 2.4 MiB 上传稳定收到响应，服务最多排空 4 MiB、总计最多等待 2 秒，每次读取不超过 64 KiB；更大或不完整的上传也不会触发无界读取。

更新包在 feed 字节数和 SHA 匹配后，仍需验证 ZIP 内部压缩流。清单或负载 deflate 损坏归 `invalid_update`；网络失败和来源 404 仍归 `network_error`。

`data/app-updates/install-lock.json` 引用缺失或无法验证的事务时，启动继续被阻止。先备份整个 `data/app-updates` 和当前程序目录，保留锁标记、transactions、transaction.json、journal.json、backups，核对事务后由维护者决定恢复；不要直接删除锁标记或覆盖程序。已有可验证的事务继续使用原有恢复逻辑。

回归入口：`python -B -m unittest discover -s tests -p test_audit_service_recovery.py -v`。仅使用临时安装和本机隔离端口，不访问正式资产或停止正式实例。
