"""Windows inbox UIAutomationClient driver. Requests are JSON on stdin, never shell code."""

from tools.computer_use.recipient_windows_clipboard import CLIPBOARD_SCRIPT

CONTROL_ROLE_SCRIPT = r'''
function ControlRole($controlType) {
 if($null -eq $controlType -or [string]::IsNullOrEmpty($controlType.ProgrammaticName)){return 'Unknown'}
 return $controlType.ProgrammaticName.Replace('ControlType.','')
}
'''

SCRIPT = r'''
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class HermesRecipientNative {
 [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h,uint m,UIntPtr w,IntPtr l,uint f,uint t,out UIntPtr r);
}
"@
$request=[Console]::In.ReadLine() | ConvertFrom-Json
$any=[System.Windows.Automation.Condition]::TrueCondition
$desc=[System.Windows.Automation.TreeScope]::Descendants
$children=[System.Windows.Automation.TreeScope]::Children
$walker=[System.Windows.Automation.TreeWalker]::ControlViewWalker
function Rid($element) { return ($element.GetRuntimeId() -join '.') }
function WindowFacts($window) {
 $p=Get-Process -Id $window.Current.ProcessId
 return @{pid=$p.Id;window_id=$window.Current.NativeWindowHandle;exe=$p.Path;
  process_started=$p.StartTime.ToUniversalTime().ToString('o');
  product=$p.MainModule.FileVersionInfo.ProductName;company=$p.MainModule.FileVersionInfo.CompanyName;
  title=$window.Current.Name}
}
function ComposerValue($composer,$value) {
 $raw=$value.Current.Value
 if($raw -ceq ("`n"+$composer.Current.Name)){
  $parts=$composer.FindAll($children,$any)
  if($parts.Count -eq 2 -and $parts[0].Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
   $parts[1].Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
   $parts[0].Current.Name -ceq "`n" -and $parts[1].Current.Name -ceq $composer.Current.Name){return ''}
 }
 return $raw
}
function NodeFacts($element) {
 $c=$element.Current;$parent=$walker.GetParent($element)
 $value=$null;$valueSupported=$element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value)
 $invoke=$null;$invokeSupported=$element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)
 $selection=$null;$selected=$false
 if($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$selection)){$selected=$selection.Current.IsSelected}
 $win=$null;$modal=$false
 if($element.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern,[ref]$win)){$modal=$win.Current.IsModal}
 $text=$null;$textSupported=$element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern,[ref]$text)
 $name=$c.Name
 return @{id=(Rid $element);parent=$(if($parent){Rid $parent}else{''});role=(ControlRole $c.ControlType);
  name=$name;automation_id=$c.AutomationId;enabled=$c.IsEnabled;offscreen=$c.IsOffscreen;selected=$selected;modal=$modal;
  value_supported=($valueSupported -and -not $value.Current.IsReadOnly);invoke_supported=$invokeSupported;
  text_supported=$textSupported;text=$name;value=$(if($valueSupported -and $c.ControlType -eq [System.Windows.Automation.ControlType]::Edit){ComposerValue $element $value}elseif($valueSupported){$value.Current.Value}else{''})}
}
function ExactWindow($target) {
 $window=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$target.window_id)
 $facts=WindowFacts $window
 foreach($key in @('pid','window_id','exe','process_started','product','company')){
  if([string]$facts[$key] -cne [string]$target.$key){throw 'recipient_window_changed'}
 }
 return $window
}
function AllNodes($window) {
 $found=$window.FindAll($desc,$any)
 if($found.Count -gt 5000){throw 'accessibility_truncated'}
 return $found
}
function FindRuntime($nodes,$id) {
 $found=@($nodes | Where-Object {(Rid $_) -ceq $id})
 if($found.Count -ne 1){throw 'recipient_control_stale'}
 return $found[0]
}
function VerifyComposer($window,$target,$expected) {
 [void](CodexTaskBinding $window $target)
 $nodes=AllNodes $window
 $composer=FindRuntime $nodes $target.composer_id
 $pane=FindRuntime $nodes $target.pane_id
 if((Rid ($walker.GetParent($composer))) -cne (Rid $pane)){throw 'recipient_pane_changed'}
 if((ControlRole $composer.Current.ControlType) -ne 'Edit' -or (ControlRole $pane.Current.ControlType) -notin @('Group','Pane','Document')){throw 'wrong_composer'}
 if($composer.Current.Name -cne $target.composer_name -or -not $composer.Current.IsEnabled -or $composer.Current.IsOffscreen){throw 'wrong_composer'}
 $value=$null
 if(-not $composer.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$value) -or $value.Current.IsReadOnly){throw 'composer_not_editable'}
 if((ComposerValue $composer $value) -cne $expected){throw 'composer_text_changed'}
 foreach($n in $nodes){
  $c=$n.Current;$modal=$null
  if((ControlRole $c.ControlType) -eq 'Unknown'){
   $unknownInvoke=$null;$unknownValue=$null
   if($n.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$unknownInvoke) -or
    ($n.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$unknownValue) -and -not $unknownValue.Current.IsReadOnly)){throw 'unsupported_accessibility'}
  }
  if(($n.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern,[ref]$modal) -and $modal.Current.IsModal) -or
   ($c.ControlType -eq [System.Windows.Automation.ControlType]::Button -and $c.Name -in @('Approve','Allow once','Allow this session','Run command','Yes, proceed'))){throw 'approval_surface_active'}
 }
 return @{composer=$composer;value=$value;nodes=$nodes}
}
try {
 switch($request.action){
  'windows' {
   $out=@()
   foreach($w in [System.Windows.Automation.AutomationElement]::RootElement.FindAll($children,$any)){
    try { $facts=WindowFacts $w } catch { continue }
    if(($facts.product -eq 'Codex' -and $facts.company -in @('OpenAI OpCo, LLC','OpenAI, L.L.C.')) -or
      ($facts.product -eq 'Claude' -and $facts.company -in @('Anthropic','Anthropic, PBC'))){$out+=$facts}
   }
   $result=@{windows=$out}
  }
  'task_identity' { $w=ExactWindow $request.target; $result=CopyTaskDeeplink $w $request.target }
  'snapshot' {
   $w=ExactWindow $request.target
   # Chromium's documented per-window accessibility handshake; no global screen-reader setting.
   $reply=[UIntPtr]::Zero
   [void][HermesRecipientNative]::SendMessageTimeout([IntPtr]$request.target.window_id,0x003D,[UIntPtr]::Zero,[IntPtr]1,2,1000,[ref]$reply)
   $binding=$null;$identityError=$null
   try {$binding=CodexTaskBinding $w $request.target}catch{$identityError=$_.Exception.Message}
   $result=@{window=(WindowFacts $w);nodes=@(AllNodes $w | ForEach-Object {NodeFacts $_});truncated=$false;
    task_binding=$binding;task_identity_error=$identityError}
  }
  'compose' {
   $w=ExactWindow $request.target
   $verified=VerifyComposer $w $request.target ''
   [Console]::Out.WriteLine('{"ready":true}'); [Console]::Out.Flush()
   if([Console]::In.ReadLine() -cne 'commit'){throw 'authorization_missing'}
   $w=ExactWindow $request.target
   $verified=VerifyComposer $w $request.target ''
   $verified.value.SetValue([string]$request.message)
   [void](VerifyComposer $w $request.target ([string]$request.message))
   $result=@{composed=$true}
  }
  'submit' {
   $w=ExactWindow $request.target
   $verified=VerifyComposer $w $request.target ([string]$request.message)
   $button=FindRuntime $verified.nodes $request.submit_id
   if((ControlRole $button.Current.ControlType) -ne 'Button' -or (Rid ($walker.GetParent($button))) -cne $request.target.pane_id -or -not $button.Current.IsEnabled -or $button.Current.IsOffscreen){throw 'submit_control_changed'}
   $invoke=$null
   if(-not $button.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)){throw 'submit_control_unsupported'}
   [Console]::Out.WriteLine('{"ready":true}'); [Console]::Out.Flush()
   if([Console]::In.ReadLine() -cne 'commit'){throw 'authorization_missing'}
   $w=ExactWindow $request.target
   $verified=VerifyComposer $w $request.target ([string]$request.message)
   $button=FindRuntime $verified.nodes $request.submit_id
   if((ControlRole $button.Current.ControlType) -ne 'Button' -or (Rid ($walker.GetParent($button))) -cne $request.target.pane_id -or -not $button.Current.IsEnabled -or $button.Current.IsOffscreen){throw 'submit_control_changed'}
   $invoke=$null
   if(-not $button.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)){throw 'submit_control_unsupported'}
   # Invoke the exact submit control: no foreground-global keyboard/Enter fallback.
   $invoke.Invoke()
   $result=@{submitted=$true}
  }
  default {throw 'unsupported_action'}
 }
 ConvertTo-Json -InputObject $result -Depth 8 -Compress
} catch {
 ConvertTo-Json -InputObject @{error=$_.Exception.Message} -Compress
 exit 1
}
'''


SCRIPT = SCRIPT.replace("$request=[Console]", CLIPBOARD_SCRIPT + CONTROL_ROLE_SCRIPT + "\n$request=[Console]")
