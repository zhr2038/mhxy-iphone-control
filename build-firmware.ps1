param([string]$UploadPort)
$ErrorActionPreference = 'Stop'
$fqbn = 'esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=16M,PSRAM=opi,PartitionScheme=default_8MB'
$sketch = Join-Path $PSScriptRoot 'firmware\mhxy_hid'
$build = Join-Path $PSScriptRoot 'build\firmware'
& arduino-cli compile --fqbn $fqbn --output-dir $build $sketch
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($UploadPort) {
    & arduino-cli upload --fqbn $fqbn --port $UploadPort --input-dir $build $sketch
    exit $LASTEXITCODE
}
