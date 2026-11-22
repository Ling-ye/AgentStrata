#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(uname -sm) == "Linux x86_64" ]] || { echo "需要 Linux x64。" >&2; exit 1; }
if [[ @DOCKER@ == 1 ]]; then
    [[ $(docker info --format '{{.OSType}}/{{.Architecture}}' 2>/dev/null) =~ ^linux/(x86_64|amd64)$ ]] &&
        docker compose version >/dev/null 2>&1 || { echo "请先准备可访问的 Linux x64 Docker Engine 和 Compose v2，然后重跑本安装包。" >&2; exit 1; }
fi
base="${XDG_DATA_HOME:-$HOME/.local/share}/agentstrata/apps/@BOT@"
mkdir -p "$base"
exec 9>"$base/install.lock"
flock 9
stage=$(mktemp -d "$base/.install-XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
installer_sha=$(sha256sum < "$0" | cut -d" " -f1)
offset=$(LC_ALL=C awk '/^__APPLICATION_PAYLOAD__$/{print NR+1;exit}' "$0")
tail -n +"$offset" "$0" > "$stage/payload.tgz"
(cd "$stage" && echo '@SHA@  payload.tgz' | sha256sum --check --status) || { echo "安装包完整性校验失败。" >&2; exit 1; }
mkdir "$stage/release"
tar -xzf "$stage/payload.tgz" -C "$stage/release"
version="$base/@SHA@"
if [[ -e "$version" ]]; then
    diff -qr --no-dereference "$stage/release" "$version" >/dev/null || { echo "已安装版本文件已改变。请先备份并移走该版本目录，再重跑安装包。" >&2; exit 1; }
else
    mv "$stage/release" "$version"
fi
record_installation() {
    local services=false
    [[ -f compose.yaml ]] && services=true
    mkdir -p "$base/receipts"
    local receipt="$base/receipts/$installer_sha.json"
    printf '{"format":"agentstrata-install/v1","application":"@BOT@","platform":"linux-x64","sha256":"%s","completed_at":%s,"services_ready":%s}\n' \
        "$installer_sha" "$(date +%s)" "$services" > "$stage/receipt"
    mv -f "$stage/receipt" "$receipt"
    printf '接收方安装记录 %s\n' "$receipt"
}
cd "$version"
if [[ -f compose.yaml ]]; then
    docker image load -i images.tar
    while read -r tag id; do
        [[ $(docker image inspect --format '{{.Id}}' "$tag") == "$id" ]] || { echo "镜像校验失败。" >&2; exit 1; }
    done < images.txt
    compose=(docker compose -p 'as-@BOT@' -f "$version/compose.yaml")
    if [[ -f receiver-setup ]]; then "${compose[@]}" run --rm --no-deps gateway configure; fi
    "${compose[@]}" up -d --pull never --no-build --wait
fi
if [[ -f client-linux ]]; then
    IFS= read -r relative < client-linux
    target="$version/$relative"
elif [[ -f entry-port ]]; then
    read -r service port < entry-port
    target="http://$("${compose[@]}" port "$service" "$port")/"
elif [[ -f site/index.html ]]; then
    target="$version/site/index.html"
else
    record_installation
    echo "应用服务已就绪。实际业务结果请在自己的渠道中核对。"
    exit 0
fi
# A stable launcher always opens the installed application. Keep arguments intact for CLI clients.
if [[ -f client-linux && -x "$target" && "$target" != *.html && "$target" != *.htm ]]; then
    printf '#!/usr/bin/env bash\nset -e\n' > "$stage/open"
    if [[ -f entry-port ]]; then
        read -r service port < entry-port
        printf 'address=$(docker compose -p %q -f %q port %q %q)\nexport AGENTSTRATA_SERVICE_URL="http://$address/"\n' \
            'as-@BOT@' "$version/compose.yaml" "$service" "$port" >> "$stage/open"
    fi
    printf 'cd -- %q || exit 1\nexec %q "$@"\n' "${target%/*}" "$target" >> "$stage/open"
elif [[ -f entry-port && ! -f client-linux ]]; then
    read -r service port < entry-port
    printf '#!/usr/bin/env bash\nset -e\naddress=$(docker compose -p %q -f %q port %q %q)\nexec xdg-open "http://$address/"\n' \
        'as-@BOT@' "$version/compose.yaml" "$service" "$port" > "$stage/open"
else
    printf '#!/usr/bin/env bash\nexec xdg-open %q\n' "$target" > "$stage/open"
fi
chmod 700 "$stage/open"
mv -f "$stage/open" "$base/open"
record_installation
printf '应用入口 %s\n启动命令 %q\n' "$target" "$base/open"
if [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then "$base/open" >/dev/null 2>&1 9>&- & fi
exit 0
__APPLICATION_PAYLOAD__
