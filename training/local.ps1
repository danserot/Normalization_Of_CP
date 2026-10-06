param(
    [ValidateSet('Build','Download','Tools','Audit','Ingest','Partition','Label','FastLabel','TaskExport','Export','Review','Baseline','TaskBaseline','Smoke','Train','Evaluate','TaskEvaluate','Select','Merge','Convert','GGUFCheck','QuarantineAudit','Quarantine','RestoreQuarantine','Status')]
    [string]$Stage = 'Status',
    [int]$Limit = 0,
    [ValidateRange(1,30)][int]$Epochs = 2,
    [ValidateSet('test','val')][string]$Split = 'test',
    [switch]$Resume,
    [switch]$Rebalance
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$privatePath = Join-Path $project 'private_training'
$publicPath = Join-Path $project 'models\training-public'
$codePath = Join-Path $PSScriptRoot 'local'
$image = 'readdocument-local-training:1'
New-Item -ItemType Directory -Force $privatePath,$publicPath | Out-Null
if ($Stage -eq 'Build') {
    docker build -f (Join-Path $codePath 'Dockerfile') -t $image $codePath
    exit $LASTEXITCODE
}
if ($Stage -eq 'Status') {
    # Reports contain aggregates only; never read datasets, manifests, raw logs,
    # model outputs, document names, or annotations through the agent's tools.
    docker ps -a --filter 'name=rd-local-' --format '{{.Names}} {{.Status}}'
    Get-ChildItem (Join-Path $privatePath 'reports') -Filter '*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('isolation.json','ingest.json','partition.json','labeling.json','fast_labels.json','dataset.json','smoke.json','training.json','baseline-test.json','trained-test.json','tasks-baseline.json','tasks-trained.json','tasks-gguf.json','merge.json','conversion.json','app_check.json','final.json') } |
        ForEach-Object {
            $report = Get-Content -LiteralPath $_.FullName -Encoding utf8 -Raw | ConvertFrom-Json
            $report.PSObject.Properties.Remove('dataset_sha256')
            $report | ConvertTo-Json -Depth 10
        }
    exit 0
}
$containerName = 'rd-local-' + $Stage.ToLower()
if ($Stage -eq 'GGUFCheck') {
    $serverName = 'rd-local-gguf-server'
    docker run --rm -d --name $serverName --network none --memory 2400m --memory-swap 2400m `
        --cap-drop ALL --security-opt no-new-privileges --log-driver none `
        --mount "type=bind,source=$privatePath\gguf,target=/trained,readonly" `
        ghcr.io/ggml-org/llama.cpp@sha256:f9115c95639e60abc09d4ea83b26fd4d56c66aa1174594393335a514da00c283 `
        -m /trained/model-q4_k_m.gguf --alias local --host 127.0.0.1 --port 8081 -c 8192 -np 1 -t 4 -ngl 0 -b 128 -ub 128 `
        --reasoning off --log-disable --no-webui | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    try {
        docker run --rm --name $containerName --network "container:$serverName" --pid "container:$serverName" `
            --memory 2g --cap-drop ALL --security-opt no-new-privileges `
            --mount "type=bind,source=$codePath,target=/app/training/local,readonly" `
            --mount "type=bind,source=$project\backend,target=/app/backend,readonly" `
            --mount "type=bind,source=$privatePath,target=/private" `
            $image python -m training.local.watch python -m training.local.evaluate_tasks --variant gguf
        $evaluationCode = $LASTEXITCODE
    } finally { docker stop $serverName | Out-Null }
    exit $evaluationCode
}
$dockerArgs = @('run','--rm','--name',$containerName,'--cap-drop','ALL','--security-opt','no-new-privileges',
    '--log-opt','max-size=2m','--log-opt','max-file=2',
    '--mount',"type=bind,source=$codePath,target=/app/training/local,readonly")
if ($Stage -in @('Download','Tools')) {
    # Deliberately no private output/input, credentials, home or Docker socket.
    $module = if ($Stage -eq 'Download') { 'training.local.download' } else { 'training.local.prepare_tools' }
    $dockerArgs += @('--mount',"type=bind,source=$publicPath,target=/resources",$image,'python','-m',$module)
} else {
    $dockerArgs += @('--network','none','--memory','6g','--memory-swap','8g',
        '--mount',"type=bind,source=$project\backend,target=/app/backend,readonly",
        '--mount',"type=bind,source=$privatePath,target=/private", 
        '--mount',"type=bind,source=$publicPath,target=/models,readonly")
    if ($Stage -in @('Ingest','Review')) {
        $dockerArgs += @('--mount',"type=bind,source=$project\files,target=/input,readonly")
    }
    if ($Stage -in @('QuarantineAudit','Quarantine','RestoreQuarantine')) {
        $sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $project 'files'))
        $targetRoot = [System.IO.Path]::GetFullPath((Join-Path $privatePath 'quarantine'))
        $workspaceRoot = [System.IO.Path]::GetFullPath($project) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $sourceRoot.StartsWith($workspaceRoot) -or -not $targetRoot.StartsWith($workspaceRoot)) {
            throw 'E_PATH_OUTSIDE_WORKSPACE'
        }
        $access = if ($Stage -eq 'QuarantineAudit') { ',readonly' } else { '' }
        $dockerArgs += @('--mount',"type=bind,source=$sourceRoot,target=/input$access")
        if ($Stage -ne 'QuarantineAudit') {
            $dockerArgs += @('--mount','type=volume,source=readdocument_proposal_data,target=/data')
        }
    }
    if ($Stage -in @('Label','Baseline','TaskBaseline','Smoke','Train','Evaluate','TaskEvaluate','Merge')) {
        $dockerArgs += @('--gpus','all')
    }
    if ($Stage -eq 'Review') {
        $dockerArgs += @('--mount','type=volume,source=readdocument_proposal_data,target=/data')
    } elseif ($Stage -in @('Export','Baseline','Evaluate')) {
        $dockerArgs += @('--mount','type=volume,source=readdocument_proposal_data,target=/data,readonly')
    }
    $dockerArgs += @($image,'python','-m')
    switch ($Stage) {
        'QuarantineAudit' { $dockerArgs += @('training.local.quarantine','audit') }
        'Quarantine' { $dockerArgs += @('training.local.quarantine','apply') }
        'RestoreQuarantine' { $dockerArgs += @('training.local.quarantine','restore') }
        'FastLabel' { $dockerArgs += 'training.local.fast_labels' }
        'TaskExport' { $dockerArgs += 'training.local.task_labels' }
        'TaskBaseline' { $dockerArgs += @('training.local.evaluate_tasks','--variant','baseline') }
        'TaskEvaluate' { $dockerArgs += @('training.local.evaluate_tasks','--variant','trained') }
        'Select' { $dockerArgs += 'training.local.select' }
        'Baseline' { $dockerArgs += @('training.local.evaluate','--variant','baseline','--split',$Split,'--database','/data/readdocument.sqlite3') }
        'Evaluate' { $dockerArgs += @('training.local.evaluate','--variant','trained','--split',$Split,'--database','/data/readdocument.sqlite3') }
        'Smoke' { $dockerArgs += @('training.local.train','--smoke') }
        'Train' { $dockerArgs += @('training.local.train','--epochs',"$Epochs"); if ($Resume) { $dockerArgs += '--resume' } }
        'Merge' { $dockerArgs += 'training.local.merge' }
        'Convert' { $dockerArgs += 'training.local.convert' }
        'Review' {
            if ($Limit -le 0) { $Limit = 30 }
            $dockerArgs += @('training.local.pipeline','import-review','--limit',"$Limit",'--database','/data/readdocument.sqlite3')
            if ($Rebalance) { $dockerArgs += '--rebalance' }
        }
        default {
            $dockerArgs += @('training.local.pipeline',$Stage.ToLower())
            if ($Limit -gt 0) { $dockerArgs += @('--limit',"$Limit") }
            if ($Stage -eq 'Export') { $dockerArgs += @('--database','/data/readdocument.sqlite3') }
        }
    }
}
if ($Stage -in @('Ingest','Label','Baseline','TaskBaseline','Smoke','Train','Evaluate','TaskEvaluate')) {
    $imageIndex = [Array]::IndexOf($dockerArgs, $image)
    $command = $dockerArgs[($imageIndex + 1)..($dockerArgs.Length - 1)]
    $dockerArgs = $dockerArgs[0..$imageIndex] + @('python','-m','training.local.watch') + $command
}
& docker @dockerArgs
exit $LASTEXITCODE
