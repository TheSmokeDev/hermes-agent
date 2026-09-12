"""Real clipboard algorithm with a compiled external clipboard API double."""
import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.computer_use.recipient_windows_clipboard import CLIPBOARD_IMPLEMENTATION

SHIM = r'''
using System;using System.Collections.Generic;using System.Runtime.InteropServices;using System.Text;
public class HermesClipboardApi {
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]public static extern IntPtr CreateWindowEx(uint e,string c,string n,uint s,int x,int y,int w,int h,IntPtr p,IntPtr m,IntPtr i,IntPtr a);
 [DllImport("user32.dll")]public static extern bool DestroyWindow(IntPtr h);
 [DllImport("user32.dll")]public static extern bool IsWindow(IntPtr h);
 [DllImport("user32.dll")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("kernel32.dll")]public static extern UIntPtr GlobalSize(IntPtr h);
 [DllImport("kernel32.dll")]public static extern IntPtr GlobalLock(IntPtr h);
 [DllImport("kernel32.dll")]public static extern bool GlobalUnlock(IntPtr h);
 [DllImport("kernel32.dll")]public static extern IntPtr GlobalAlloc(uint f,UIntPtr n);
 [DllImport("kernel32.dll")]public static extern IntPtr GlobalFree(IntPtr h);
 static readonly Dictionary<uint,IntPtr> clipboard=new Dictionary<uint,IntPtr>();
 static IntPtr openOwner,owner;static bool opened;static uint sequence=1;
 public static bool OpenClipboard(IntPtr h){if(opened)return false;opened=true;openOwner=h;return true;}
 public static bool CloseClipboard(){opened=false;return true;}
 public static uint GetClipboardSequenceNumber(){return sequence;}
 public static IntPtr GetClipboardOwner(){return owner;}
 public static uint EnumClipboardFormats(uint prior){
  var keys=new List<uint>(clipboard.Keys);keys.Sort();
  foreach(uint key in keys)if(key>prior)return key;return 0;
 }
 public static IntPtr GetClipboardData(uint f){IntPtr h;return clipboard.TryGetValue(f,out h)?h:IntPtr.Zero;}
 public static bool EmptyClipboard(){
  if(!opened)return false;
  foreach(IntPtr h in clipboard.Values)GlobalFree(h);
  clipboard.Clear();owner=openOwner;sequence++;return true;
 }
 public static IntPtr SetClipboardData(uint f,IntPtr h){
  if(!opened||owner==IntPtr.Zero||!IsWindow(owner))return IntPtr.Zero;
  clipboard[f]=h;sequence++;return h;
 }
}
public class ClipboardFixture : HermesClipboardApi {
 static IntPtr fixtureOwner=CreateWindowEx(0,"STATIC","fixture clipboard owner",0,0,0,0,0,new IntPtr(-3),IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
 static void Put(uint f,byte[] bytes){
  IntPtr h=GlobalAlloc(2,(UIntPtr)bytes.Length),p=GlobalLock(h);
  Marshal.Copy(bytes,0,p,bytes.Length);GlobalUnlock(h);
  if(SetClipboardData(f,h)==IntPtr.Zero)throw new Exception("null_owner_rejected");
 }
 public static void Seed(string text,bool custom){
  if(!IsWindow(fixtureOwner)||!OpenClipboard(fixtureOwner))throw new Exception("fixture_owner_failed");
  try{EmptyClipboard();Put(13,Encoding.Unicode.GetBytes(text+"\0"));if(custom)Put(50001,new byte[]{0,1,128,255,42});}
  finally{CloseClipboard();}
 }
 public static bool Matches(string text,bool custom){
  IntPtr h=GetClipboardData(13),p=GlobalLock(h);string value;
  try{value=Marshal.PtrToStringUni(p);}finally{GlobalUnlock(h);}
  if(value!=text)return false;if(!custom)return true;
  h=GetClipboardData(50001);p=GlobalLock(h);if(p==IntPtr.Zero)return false;
  try{byte[] bytes={0,1,128,255,42};for(int i=0;i<bytes.Length;i++)if(Marshal.ReadByte(p,i)!=bytes[i])return false;return true;}
  finally{GlobalUnlock(h);}
 }
}
'''


@pytest.mark.windows_only
@pytest.mark.parametrize("user_changed", [False, True])
def test_clipboard_owned_hwnd_restores_bytes_and_preserves_new_user_copy(user_changed):
    from hermes_cli._subprocess_compat import windows_hide_flags
    changed = "$true" if user_changed else "$false"
    script = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\n"
              + 'Add-Type -TypeDefinition @"\n' + SHIM + CLIPBOARD_IMPLEMENTATION + '\n"@\n') + r'''
[ClipboardFixture]::Seed('original private test text',$true)
$backup=New-Object HermesClipboardBackup
try {
 $backup.AssertUnchanged()
 [ClipboardFixture]::Seed('codex://threads/11111111-2222-4333-8444-555555555555',$false)
 if($backup.ReadCopy([uint32]$PID) -cne 'codex://threads/11111111-2222-4333-8444-555555555555'){throw 'copy_read_failed'}
 if(CHANGED){[ClipboardFixture]::Seed('new user test copy',$false)}
 $restored=$backup.RestoreIfUnchanged()
 $matches=if(CHANGED){[ClipboardFixture]::Matches('new user test copy',$false)}else{[ClipboardFixture]::Matches('original private test text',$true)}
 @{restored=$restored;matches=$matches;formats=$backup.FormatCount}|ConvertTo-Json -Compress
}finally{$backup.Dispose()}
'''.replace("CHANGED", changed)
    bootstrap = ("[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false);"
                 "Invoke-Expression ([Text.Encoding]::Unicode.GetString("
                 "[Convert]::FromBase64String([Console]::In.ReadLine())))")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
         base64.b64encode(bootstrap.encode("utf-16le")).decode()],
        input=base64.b64encode(script.encode("utf-16le")).decode() + "\n",
        text=True, encoding="utf-8", capture_output=True, timeout=20, creationflags=windows_hide_flags())
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["matches"] and receipt["formats"] == 2
    assert receipt["restored"] is (not user_changed)


MENU_SHIM = r'''
using System;
public class HermesClipboardApi {
 public static long[] TopLevelWindows(){return new long[]{100,200,201,300,400,500,600};}
 public static bool IsWindowVisible(IntPtr h){return h.ToInt64()!=500;}
 public static uint GetWindowThreadProcessId(IntPtr h,out uint p){p=h.ToInt64()==300?8u:7u;return 1;}
 public static IntPtr GetWindow(IntPtr h,uint command){
  long n=h.ToInt64();return new IntPtr(n==200||n==300||n==500||n==600?100:n==201?200:n==400?999:0);
 }
}
public class HermesClipboardBackup : IDisposable {
 public int FormatCount {get{return 2;}}
 public void AssertUnchanged(){}
 public string ReadCopy(uint pid){return "codex://threads/11111111-2222-4333-8444-555555555555";}
 public bool RestoreIfUnchanged(){return true;}
 public void Dispose(){}
}
'''


@pytest.mark.windows_only
def test_task_menu_owned_popup_identity_and_expansion():
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.computer_use.recipient_windows_clipboard import CLIPBOARD_FUNCTIONS
    script = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\n"
              "Add-Type -AssemblyName UIAutomationClient\nAdd-Type -AssemblyName UIAutomationTypes\n"
              + 'Add-Type -TypeDefinition @"\n' + MENU_SHIM + '\n"@\n'
              + CLIPBOARD_FUNCTIONS + r'''
$desc=[System.Windows.Automation.TreeScope]::Descendants
$target=@{pid=7;window_id=100}
function Rid($element){return [string]$element.Id}
function ExactWindow($target){return $script:main}
function Element($id,$name,$role){
 $e=[pscustomobject]@{Id=$id;Current=[pscustomobject]@{Name=$name;ControlType=$role;IsOffscreen=$false;IsEnabled=$true;ProcessId=7};Nodes=@();Pattern=$null}
 $e|Add-Member ScriptMethod FindAll {param($scope,$condition);return @($this.Nodes|Where-Object {$_.Current.Name -ceq $condition.Value})}
 $e|Add-Member ScriptMethod GetCurrentPattern {param($pattern);return $this.Pattern}
 return $e
}
function TaskMenuPopupElement($handle){$script:opened+=[long]$handle;return $script:popups[[long]$handle]}
function Pattern($expanded){
 $p=[pscustomobject]@{Current=[pscustomobject]@{ExpandCollapseState=$(if($expanded){[System.Windows.Automation.ExpandCollapseState]::Expanded}else{[System.Windows.Automation.ExpandCollapseState]::Collapsed})};Expands=0;Invokes=0}
 $p|Add-Member ScriptMethod Expand {$this.Expands++;$this.Current.ExpandCollapseState=[System.Windows.Automation.ExpandCollapseState]::Expanded}
 $p|Add-Member ScriptMethod Collapse {}
 $p|Add-Member ScriptMethod Invoke {$this.Invokes++}
 return $p
}
$menuType=[System.Windows.Automation.ControlType]::MenuItem
$answers=@{}
foreach($mode in @('main','owned','nested','duplicate','ambiguous','foreign')){
 $script:opened=@();$script:main=Element 100 '' ([System.Windows.Automation.ControlType]::Window)
 $script:popups=@{}
 foreach($id in @(200,201,300,400,500,600)){$script:popups[[long]$id]=Element $id '' ([System.Windows.Automation.ControlType]::Menu)}
 $script:popups[[long]600].Current.ProcessId=8
 $one=Element 1 'Copy' $menuType;$two=Element 2 'Copy' $menuType
 switch($mode){
  main {$script:main.Nodes=@($one)}
  owned {$script:popups[[long]200].Nodes=@($one)}
  nested {$script:popups[[long]201].Nodes=@($one)}
  duplicate {$script:main.Nodes=@($one);$script:popups[[long]200].Nodes=@($one)}
  ambiguous {$script:main.Nodes=@($one);$script:popups[[long]200].Nodes=@($two)}
  foreign {foreach($id in @(300,400,500,600)){$script:popups[[long]$id].Nodes=@($one)}}
 }
 try {$found=FindVisibleNamed $script:main 'Copy' $menuType $target;$answers[$mode]=[string]$found.Id}
 catch {$answers[$mode]=$_.Exception.Message}
 if(@($script:opened|Where-Object {$_ -in @(300,400,500)}).Count){throw 'foreign_popup_read'}
}
$expansions=@()
foreach($expanded in @($false,$true)){
 $script:main=Element 100 '' ([System.Windows.Automation.ControlType]::Window)
 foreach($popup in $script:popups.Values){$popup.Nodes=@()}
 $button=Element 10 'Chat actions' ([System.Windows.Automation.ControlType]::Button);$button.Pattern=Pattern $expanded
 $copy=Element 11 'Copy' $menuType;$copy.Pattern=Pattern $expanded
 $link=Element 12 'Copy deeplink Alt+Ctrl+L' $menuType;$link.Pattern=Pattern $false
 $script:main.Nodes=@($button);$script:popups[[long]200].Nodes=@($copy,$link)
 $result=CopyTaskDeeplink $script:main $target
 $expansions+=@{actions=$button.Pattern.Expands;copy=$copy.Pattern.Expands;invoked=$link.Pattern.Invokes;restored=$result.clipboard_restored}
}
@{lookups=$answers;expansions=$expansions}|ConvertTo-Json -Depth 4 -Compress
''')
    bootstrap = ("[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false);"
                 "Invoke-Expression ([Text.Encoding]::Unicode.GetString("
                 "[Convert]::FromBase64String([Console]::In.ReadLine())))")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
         base64.b64encode(bootstrap.encode("utf-16le")).decode()],
        input=base64.b64encode(script.encode("utf-16le")).decode() + "\n", capture_output=True,
        text=True, encoding="utf-8", timeout=15, creationflags=windows_hide_flags())
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["lookups"] == {"main": "1", "owned": "1", "nested": "1", "duplicate": "1",
                                "ambiguous": "task_deeplink_ambiguous",
                                "foreign": "task_deeplink_unavailable_Copy"}
    assert value["expansions"] == [
        {"actions": 1, "copy": 1, "invoked": 1, "restored": True},
        {"actions": 0, "copy": 0, "invoked": 1, "restored": True}]
