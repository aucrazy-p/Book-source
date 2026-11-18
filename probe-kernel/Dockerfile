# L4 一次性探测容器（方案 §11 沙箱与安全边界）
#
# 目标：把不可信书源 JS 关进用完即弃的容器，与 GITHUB_TOKEN / Secret 物理隔离。
# 本镜像只提供运行底座；probe-kernel 二进制由 daily.yml 从 Release 下载后挂载执行，
# 不在镜像里固化二进制（构建 workflow 单独产出，见 build-kernel.yml）。
#
# docker run 时由调用方施加：非 root、只读根、tmpfs(/tmp)、内存/PID/CPU 限制、超时。
#
# 二进制是 musl 静态编译（build-kernel.yml 用 x86_64-unknown-linux-musl target），
# 不依赖 glibc，因此不受 runner 滚动升级（ubuntu-latest 已是 26.04）与容器版本
# 错配影响——这是方案 §11「沙箱稳定」的前提。容器只需提供 CA 证书与运行身份。
FROM alpine:3.20

# ca-certificates：书源 HTTPS 请求需要根证书；tzdata 让时间/日志正常。
RUN apk add --no-cache ca-certificates tzdata \
 && adduser -u 10001 -D -s /sbin/nologin probe

# 探测产物与 JS store 都落 /tmp（运行时挂 tmpfs），无状态、用完即弃。
ENV TMPDIR=/tmp
USER probe
WORKDIR /work

ENTRYPOINT ["/usr/local/bin/probe-kernel"]
