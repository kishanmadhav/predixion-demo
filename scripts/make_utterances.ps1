# Synthesize the demo caller utterances as 16 kHz mono 16-bit WAV with Windows' built-in
# speech engine (no network, no model). Run once:  powershell -File scripts/make_utterances.ps1
Add-Type -AssemblyName System.Speech
$out = Join-Path $PSScriptRoot "..\assets\utterances"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$lines = @(
  "I can pay half now and the rest next Friday.",
  "Who is this calling?",
  "I already paid this last week.",
  "Can I speak to a real person?",
  "I lost my job, I need more time.",
  "Okay, what is the balance?"
)
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 1
foreach ($line in $lines) {
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $path = Join-Path $out ("u{0}.wav" -f $i)
  $synth.SetOutputToWaveFile($path, $format)
  $synth.Speak($line)
  $synth.Dispose()
  Write-Output ("wrote {0}" -f $path)
  $i++
}
