$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$socket = $null
$deadline = $null
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $connectionFile = [string]$request.connection_file
    if ($request.select) {
        Add-Type -AssemblyName System.Windows.Forms
        $picker = New-Object System.Windows.Forms.FolderBrowserDialog
        $picker.Description = '选择正在使用的 AgentStrata 桌宠应用目录'
        $picker.SelectedPath = Join-Path $env:USERPROFILE '.agentstrata\applications\live2d-desktop-native'
        $picker.ShowNewFolderButton = $false
        try {
            if ($picker.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) {
                [Console]::Out.Write('{"canceled":true}'); exit 0
            }
            $connectionFile = Join-Path $picker.SelectedPath '.agentstrata-data\gateway\studio-connection.json'
        } finally { $picker.Dispose() }
    }
    if (-not [IO.Path]::IsPathRooted($connectionFile)) { throw 'Invalid connection location' }
    $item = Get-Item -LiteralPath $connectionFile
    if ($item.Length -gt 8192 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid connection file' }
    $connection = [IO.File]::ReadAllText($connectionFile) | ConvertFrom-Json
    if ($connection.schema -ne 'agentstrata-studio-connection/v1' -or $connection.id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid connection identity' }
    if ($request.expected_id -and $request.expected_id -cne $connection.id) { throw 'Application identity changed' }
    $endpoint = [Uri]$connection.url
    if ($endpoint.Scheme -ne 'ws' -or $endpoint.Host -ne '127.0.0.1' -or $endpoint.AbsolutePath -ne '/' -or $endpoint.Query -or $endpoint.UserInfo) { throw 'Invalid local endpoint' }
    $deadline = New-Object Threading.CancellationTokenSource
    $deadline.CancelAfter(30000)
    $socket = New-Object Net.WebSockets.ClientWebSocket
    $socket.Options.Proxy = $null
    [void]$socket.ConnectAsync($endpoint, $deadline.Token).GetAwaiter().GetResult()
    function Send-Frame($frame) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($frame | ConvertTo-Json -Depth 24 -Compress))
        [void]$socket.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $deadline.Token).GetAwaiter().GetResult()
    }
    function Read-Frame {
        $buffer = New-Object byte[] 65536
        $stream = New-Object IO.MemoryStream
        try {
            do {
                $part = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $deadline.Token).GetAwaiter().GetResult()
                if ($part.MessageType -ne [Net.WebSockets.WebSocketMessageType]::Text) { throw 'Connection closed' }
                $stream.Write($buffer, 0, $part.Count)
                if ($stream.Length -gt 1048576) { throw 'Gateway frame too large' }
            } while (-not $part.EndOfMessage)
            return ([Text.Encoding]::UTF8.GetString($stream.ToArray()) | ConvertFrom-Json)
        } finally { $stream.Dispose() }
    }
    $challenge = Read-Frame
    if ($challenge.event -ne 'connect.challenge') { throw 'Gateway challenge missing' }
    Send-Frame @{type='req'; id='connect'; method='connect'; params=@{
        nonce=$challenge.payload.nonce; minProtocol=1; maxProtocol=1;
        client=@{id='studio-manager'; version='1.0.0'; mode='management'};
        scopes=@('application.read','application.settings'); capabilities=@(); auth=@{token=$connection.token}
    }}
    $hello = Read-Frame
    if (-not $hello.ok -or $hello.id -ne 'connect' -or 'application.read' -notin $hello.result.methods) { throw 'Management is unavailable' }
    Send-Frame @{type='req'; id='identity-check'; method='application.read'; params=@{operation='identity'}}
    $identityReply = Read-Frame
    if (-not $identityReply.ok -or $identityReply.id -ne 'identity-check') { throw 'Application identity unavailable' }
    $actualIdentity = $identityReply.result.content | ConvertFrom-Json
    if ($actualIdentity.id -cne $connection.id) { throw 'Connected application identity changed' }
    if ($request.write) {
        Send-Frame @{type='req'; id='save'; method='application.settings'; idempotencyKey=$request.request_id; params=$request.write}
        $reply = Read-Frame
        if (-not $reply.ok) {
            if ($reply.error.code -eq 'settings_conflict') { [Console]::Out.Write('{"code":"settings_conflict","error":"配置已被更新，请重新读取后保存。"}'); exit 0 }
            throw 'Settings save failed'
        }
        $result = $reply.result
    } else {
        $arguments = @{}
        if ($request.arguments) { foreach ($property in $request.arguments.PSObject.Properties) { $arguments[$property.Name] = $property.Value } }
        $content = New-Object Text.StringBuilder
        $digest = $null
        do {
            Send-Frame @{type='req'; id=('read-' + $content.Length); method='application.read'; params=@{operation=$request.operation; arguments=$arguments; offset=$content.Length; snapshot=$(if ($digest) { $digest } else { '' })}}
            $reply = Read-Frame
            if (-not $reply.ok) { throw 'Observation unavailable' }
            $chunk = $reply.result
            if ($chunk.total -gt 16777216 -or ($digest -and $digest -cne $chunk.digest)) { throw 'Observation changed; retry' }
            if (-not $chunk.content -and $content.Length -lt $chunk.total) { throw 'Incomplete observation' }
            $digest = $chunk.digest
            [void]$content.Append($chunk.content)
        } while ($content.Length -lt $chunk.total)
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $actual = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($content.ToString()))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        if ($actual -cne $digest) { throw 'Observation checksum mismatch' }
        $result = $content.ToString() | ConvertFrom-Json
        if ($request.operation -eq 'identity' -and $result.id -cne $connection.id) { throw 'Application identity changed' }
    }
    [Console]::Out.Write((@{connection_file=$connectionFile; result=$result} | ConvertTo-Json -Depth 100 -Compress))
} catch {
    [Console]::Error.Write('Native application management is unavailable. Check the selected application and its running client.')
    exit 1
} finally {
    if ($socket) { $socket.Dispose() }
    if ($deadline) { $deadline.Dispose() }
}
