<a name="build-and-publish-a-release"></a>

# 构建与发布

GitHub Actions 在 Windows Server 2025 上生成 Windows x64 便携 ZIP 和 MSI，在 Ubuntu 26.04 上生成 amd64 DEB。Windows 11 桌面验收使用便携 ZIP。

<a name="continuous-integration"></a>

## 持续集成

- `ci.yml` 接受分支推送、拉取请求和手动触发，并在这些运行中检查文档标点。
- Release 调用同一个 `ci.yml` 运行构建与验收作业。每次运行使用 `.nvmrc` 指定的 Node 构建一次前端。两个平台使用同一份 `web-dist` 编译、测试、安装和打包原生服务，并运行 MCP SDK 测试。
- Windows 验证本次构建的 ZIP。手动 CI 和 Release 还会使用同一份 MSI，依次检查默认及自定义安装目录、凭据与配置保留，并分别在初始化后和服务启动后触发故障，验证安装事务回滚。
- Ubuntu 验证本次构建的 DEB，包括同包重装、服务故障恢复，以及 `remove` 和 `purge` 对运行配置、凭据和数据的处理。
- main 的 push CI 和手动 CI 还会在 Ubuntu 上执行 ASan、UBSan、clang-tidy 和限时模糊测试。MSI 生命周期测试在具有管理员权限的可丢弃环境中执行。

本地构建流程的 `publish` 阶段只生成安装包。[构建说明](build.md)

<a name="publish-from-a-version-tag"></a>

## 通过版本标签发布

先在候选分支验证发布提交，再将同一提交推送到 main。`release.yml` 会核对标签与 `QCWS_VERSION`，并通过 `scripts/release_gate.py` 确认标签提交已属于 main 且该 SHA 最新一次 main push CI 成功。发布包由标签工作流重新构建和验收。

以下命令在仓库根目录的同一个 PowerShell 7 终端中执行。准备好已登录的 GitHub CLI 和所需仓库权限。[GitHub CLI](https://cli.github.com/)

### 1. 验证候选分支

在独立分支提交已审查的改动，完成本地验证。确认工作区干净，原生程序和内嵌前端的版本一致。

```powershell
$ErrorActionPreference = 'Stop'
$repo = 'xiaobing318/QCopilots-WebSearch'
$version = '0.5.1'
$tag = "v$version"
$branch = (git branch --show-current).Trim()
if (!$branch -or $branch -eq 'main') { throw '请在候选分支上执行。' }
if (git status --porcelain) { throw '请先审查并提交发布改动。' }
$releaseSha = (git rev-parse HEAD).Trim()
$native = Select-String -LiteralPath CMakeLists.txt -Pattern '^set\(QCWS_VERSION "([0-9]+\.[0-9]+\.[0-9]+)"'
$web = Get-Content -Raw -LiteralPath web/package.json | ConvertFrom-Json
$npmLock = Get-Content -Raw -LiteralPath web/package-lock.json | ConvertFrom-Json -AsHashtable
if (!$native -or $native.Matches[0].Groups[1].Value -ne $version -or
    $web.version -ne $version -or $npmLock.version -ne $version -or $npmLock.packages[''].version -ne $version) {
    throw 'CMake、前端清单或锁文件中的项目版本与标签不一致。'
}
git push --set-upstream origin $branch
if ($LASTEXITCODE -ne 0) { throw '候选分支推送失败。' }
```

等分支推送触发的 CI 结束，再为该分支手动运行 CI。同一分支的两种 CI 共用并发组。以下函数核对运行名称、触发事件、分支或标签及提交 SHA，然后等待成功。[手动触发](https://cli.github.com/manual/gh_workflow_run) · [跟踪运行](https://cli.github.com/manual/gh_run_watch)

```powershell
function Watch-CheckedRun($runId, $workflowName, $trigger, $expectedRef) {
    $run = gh run view $runId --repo $repo --json workflowName,event,headBranch,headSha | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $run.workflowName -ne $workflowName -or $run.event -ne $trigger -or
        $run.headBranch -cne $expectedRef -or $run.headSha -ne $releaseSha) {
        throw '选择的 Actions 运行与工作流、引用或发布提交不一致。'
    }
    gh run watch $runId --repo $repo --exit-status
    if ($LASTEXITCODE -ne 0) { throw 'Actions 运行未成功。' }
}
gh workflow run ci.yml --repo $repo --ref $branch
if ($LASTEXITCODE -ne 0) { throw '候选分支手动 CI 触发失败。' }
gh run list --repo $repo --workflow ci.yml --branch $branch --event workflow_dispatch --commit $releaseSha --limit 5 --json databaseId,createdAt,url
$deepRunId = Read-Host '输入刚触发的手动运行 ID，列表未更新时请先重新查询'
Watch-CheckedRun $deepRunId 'CI' 'workflow_dispatch' $branch
```

在 Windows 11 上验收该运行的 `packages-windows-x64`。其中包含 MSI 和便携 ZIP，桌面测试使用内层便携 ZIP。记录包的 SHA-256 和运行 ID，按部署指引验证 MCP 工具发现、搜索、网页打开和查找。验收结束后关闭本次启动的进程。[部署指引](deployment.md)

### 2. 推送同一提交到 main

以下命令执行快进推送。若远端 main 含有候选提交尚未包含的改动，请整合后重新验证新的发布 SHA。需要通过拉取请求合入时，也应核对最终提交并为该 SHA 完成验证。

```powershell
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw '获取 main 失败。' }
git merge-base --is-ancestor origin/main $releaseSha
if ($LASTEXITCODE -ne 0) { throw '候选提交无法快进 main。' }
git push origin "${releaseSha}:refs/heads/main"
if ($LASTEXITCODE -ne 0) { throw '推送 main 失败。' }
gh run list --repo $repo --workflow ci.yml --branch main --event push --commit $releaseSha --limit 1 --json databaseId,headSha,url
$pushRunId = Read-Host '输入本提交的 main push CI 运行 ID，列表未更新时请先重新查询'
Watch-CheckedRun $pushRunId 'CI' 'push' 'main'
```

### 3. 创建带注释的版本标签

使用版本号作为标签注释。GitHub Release 标题使用版本号，页面正文留空。发布命令写入不可见的空 HTML 注释，避免 GitHub 在正文为空时显示标签注释。推送前核对该 SHA 最新的 main push CI、标签类型和目标提交。

```powershell
$gate = @(gh run list --repo $repo --workflow ci.yml --branch main --event push --commit $releaseSha --limit 1 --json headSha,status,conclusion | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $gate.Count -ne 1 -or $gate[0].headSha -ne $releaseSha -or
    $gate[0].status -ne 'completed' -or $gate[0].conclusion -ne 'success') {
    throw '本提交最新的 main push CI 尚未成功。'
}
git tag -a -m $tag $tag $releaseSha
if ($LASTEXITCODE -ne 0) { throw '创建注释标签失败，请检查已有标签。' }
$tagRef = "refs/tags/$tag"
$tagType = git cat-file -t $tagRef
if ($LASTEXITCODE -ne 0 -or $tagType -cne 'tag') { throw '需要带注释的标签。' }
$tagCommit = git rev-parse --verify "$tagRef^{commit}"
if ($LASTEXITCODE -ne 0 -or $tagCommit -cne $releaseSha) { throw '标签指向的提交与发布 SHA 不一致。' }
git push origin "refs/tags/$tag"
if ($LASTEXITCODE -ne 0) { throw '推送标签失败，请检查远端状态。' }
```

标签推送会触发 Release。安装验收成功后，工作流校验锁定的 Qt 源码，生成 `SHA256SUMS`，为三个安装包签发构建来源证明，并用 `gh release create --verify-tag --title "$GITHUB_REF_NAME" --notes '<!-- -->'` 发布。[GitHub Release 命令](https://cli.github.com/manual/gh_release_create)

### 4. 验证发布页、资产与来源证明

```powershell
gh run list --repo $repo --workflow release.yml --event push --commit $releaseSha --limit 5 --json databaseId,headBranch,headSha,url
$releaseRunId = Read-Host '输入本标签的 Release 运行 ID，列表未更新时请先重新查询'
Watch-CheckedRun $releaseRunId 'Release' 'push' $tag
$remoteTag = git ls-remote --exit-code --tags origin "refs/tags/$tag^{}"
if ($LASTEXITCODE -ne 0 -or ($remoteTag -split '\s+')[0] -ne $releaseSha) { throw '远端注释标签指向其他提交。' }
$release = gh release view $tag --repo $repo --json tagName,name,body,isDraft,isPrerelease | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $release.tagName -cne $tag -or $release.isDraft -or $release.isPrerelease) {
    throw '发布页的标签或正式发布状态不符。'
}
if ($release.name -cne $tag -or $release.body -cne '<!-- -->') { throw 'Release 应显示版本号标题和空白正文。' }
$latest = gh api "repos/$repo/releases/latest" --jq '.tag_name'
if ($LASTEXITCODE -ne 0 -or $latest -cne $tag) { throw 'Latest 未指向本次正式发布。' }
$releaseDir = Join-Path ([IO.Path]::GetTempPath()) "qcopilots-$tag-$([guid]::NewGuid())"
gh release download $tag --repo $repo --dir $releaseDir
if ($LASTEXITCODE -ne 0) { throw '下载发布资产失败。' }
```

下载目录应包含三个安装包、匹配的 Qt 源码和 `SHA256SUMS`，共五项资产。清单须分别覆盖四个有效载荷。Qt 源码另与发布提交的依赖锁核对，三个安装包的来源证明分别绑定到发布工作流、标签和提交。[下载资产](https://cli.github.com/manual/gh_release_download) · [验证来源证明](https://cli.github.com/manual/gh_attestation_verify)

```powershell
$packages = @("QCopilots-WebSearch-$version-win64.zip", "QCopilots-WebSearch-$version-win64.msi",
              "qcopilots-websearch-${version}_amd64.deb")
$lock = git show "${releaseSha}:config/dependencies.lock.json" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '读取发布提交的依赖锁失败。' }
$qtName = [IO.Path]::GetFileName(([uri]$lock.qt_source.url).AbsolutePath)
$payloads = $packages + $qtName
$expectedAssets = $payloads + 'SHA256SUMS'
$assets = @(Get-ChildItem -LiteralPath $releaseDir -File | Select-Object -ExpandProperty Name)
if ($assets.Count -ne 5 -or (Compare-Object -CaseSensitive $expectedAssets $assets)) { throw '发布资产名称或数量不符。' }

$digests = @{}
foreach ($line in Get-Content -LiteralPath (Join-Path $releaseDir 'SHA256SUMS')) {
    if ($line -cnotmatch '^([0-9a-f]{64})  ([^/\\]+)$') { throw '校验清单格式不正确。' }
    $digest, $name = $Matches[1], $Matches[2]
    if ($payloads -cnotcontains $name -or $digests.ContainsKey($name)) { throw "意外或重复的清单条目 $name" }
    $actual = (Get-FileHash -LiteralPath (Join-Path $releaseDir $name) -Algorithm SHA256).Hash
    if ($actual -ne $digest) { throw "文件校验失败 $name" }
    $digests[$name] = $digest
}
if ($digests.Count -ne 4 -or $digests[$qtName] -ne $lock.qt_source.sha256) { throw '清单不完整或 Qt 源码与锁文件不一致。' }
foreach ($name in $packages) {
    gh attestation verify (Join-Path $releaseDir $name) --repo $repo --signer-workflow "$repo/.github/workflows/release.yml" --source-ref "refs/tags/$tag" --source-digest $releaseSha
    if ($LASTEXITCODE -ne 0) { throw "来源证明验证失败 $name" }
}
```

记录 `$releaseSha`、`$deepRunId`、`$pushRunId`、`$releaseRunId`、下载文件及校验结果。[GitHub Releases](https://github.com/xiaobing318/QCopilots-WebSearch/releases)

### 失败处理

- 用 `gh run view <运行ID> --repo $repo --log-failed` 检查失败步骤。临时基础设施或下载故障可重跑同一提交的工作流。需要修改代码时，请对新提交重新执行相应验收。
- main push CI 失败或被取消时，等待该提交的 main push CI 成功后再打标签。若标签工作流已因门禁失败，待同一 SHA 的 main push CI 成功后重跑该 Release。
- 已推送的标签固定其发布提交。若修复需要修改已打标签的源码，请发布新的补丁版本。若已创建 Release，先核对该页现有资产、来源证明和工作流日志，保留已发布标签及资产。

参阅[许可声明和 Qt 替换说明](licensing.md)。
