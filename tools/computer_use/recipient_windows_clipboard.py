"""Native clipboard preservation for the existing-task Copy deeplink command."""

CLIPBOARD_NATIVE_API = r'''
using System; using System.Collections.Generic; using System.Runtime.InteropServices;
public class HermesClipboardApi {
 public delegate bool EnumerateWindow(IntPtr h,IntPtr p);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumerateWindow callback,IntPtr p);
 [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h,uint command);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 public static long[] TopLevelWindows(){
  var windows=new List<long>();
  EnumWindows((h,p)=>{windows.Add(h.ToInt64());return true;},IntPtr.Zero);
  return windows.ToArray();
 }
 [DllImport("user32.dll",SetLastError=true)] public static extern bool OpenClipboard(IntPtr h);
 [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern IntPtr CreateWindowEx(uint ex,string cls,string title,uint style,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr param);
 [DllImport("user32.dll")] public static extern bool DestroyWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool CloseClipboard();
 [DllImport("user32.dll",SetLastError=true)] public static extern uint EnumClipboardFormats(uint f);
 [DllImport("user32.dll")] public static extern IntPtr GetClipboardData(uint f);
 [DllImport("user32.dll")] public static extern IntPtr GetClipboardOwner();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
 [DllImport("user32.dll")] public static extern bool EmptyClipboard();
 [DllImport("user32.dll")] public static extern IntPtr SetClipboardData(uint f,IntPtr h);
 [DllImport("kernel32.dll")] public static extern UIntPtr GlobalSize(IntPtr h);
 [DllImport("kernel32.dll")] public static extern IntPtr GlobalLock(IntPtr h);
 [DllImport("kernel32.dll")] public static extern bool GlobalUnlock(IntPtr h);
 [DllImport("kernel32.dll")] public static extern IntPtr GlobalAlloc(uint flags,UIntPtr bytes);
 [DllImport("kernel32.dll")] public static extern IntPtr GlobalFree(IntPtr h);
}

'''

CLIPBOARD_IMPLEMENTATION = r'''
public sealed class HermesClipboardBackup : HermesClipboardApi, IDisposable {
 sealed class Saved { public uint Format; public IntPtr Handle; public byte[] Bytes; }
 readonly List<Saved> saved=new List<Saved>();
 readonly uint original; uint copied; IntPtr owner;
 public int FormatCount {get{return saved.Count;}}
 void Open() {if(!OpenClipboard(owner))throw new InvalidOperationException("clipboard_busy");}
 public HermesClipboardBackup() {
  owner=CreateWindowEx(0,"STATIC","Hermes clipboard backup",0,0,0,0,0,new IntPtr(-3),IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
  if(owner==IntPtr.Zero)throw new InvalidOperationException("clipboard_owner_unavailable");
  try { Open(); try {
   original=GetClipboardSequenceNumber(); uint f=0; ulong total=0;
   while((f=EnumClipboardFormats(f))!=0) {
    if(f==2||f==3||f==9||f==14||(f>=0x80&&f<=0x8f)||(f>=0x300&&f<=0x3ff))
     throw new InvalidOperationException("clipboard_format_not_preservable");
    IntPtr src=GetClipboardData(f); ulong size=GlobalSize(src).ToUInt64(); total+=size;
    if(src==IntPtr.Zero||size==0||total>67108864)
     throw new InvalidOperationException("clipboard_format_not_preservable");
    IntPtr memory=GlobalLock(src);
    if(memory==IntPtr.Zero)throw new InvalidOperationException("clipboard_format_not_preservable");
    byte[] bytes=new byte[(int)size];
    try {Marshal.Copy(memory,bytes,0,bytes.Length);} finally {GlobalUnlock(src);}
    IntPtr clone=GlobalAlloc(2,(UIntPtr)size);
    if(clone==IntPtr.Zero)throw new InvalidOperationException("clipboard_backup_failed");
    IntPtr dest=GlobalLock(clone);
    if(dest==IntPtr.Zero){GlobalFree(clone);throw new InvalidOperationException("clipboard_backup_failed");}
    try {Marshal.Copy(bytes,0,dest,bytes.Length);} finally {GlobalUnlock(clone);}
    saved.Add(new Saved{Format=f,Handle=clone,Bytes=bytes});
   }
  } finally {CloseClipboard();} } catch {Dispose();throw;}
 }
 public void AssertUnchanged() {
  Open();try {if(GetClipboardSequenceNumber()!=original)throw new InvalidOperationException("clipboard_changed");}
  finally {CloseClipboard();}
 }
 public string ReadCopy(uint expectedPid) {
  Open();
  try {
   uint seq=GetClipboardSequenceNumber();
   if(seq==original)return null;
   uint owner; GetWindowThreadProcessId(GetClipboardOwner(),out owner);
   if(owner!=expectedPid)throw new InvalidOperationException("clipboard_changed");
   copied=seq;
   IntPtr h=GetClipboardData(13); ulong size=GlobalSize(h).ToUInt64();
   if(h==IntPtr.Zero||size<2||size>8192)throw new InvalidOperationException("task_deeplink_unavailable");
   IntPtr p=GlobalLock(h); if(p==IntPtr.Zero)throw new InvalidOperationException("task_deeplink_unavailable");
   try {return Marshal.PtrToStringUni(p,checked((int)size/2)).TrimEnd('\0');}
   finally {GlobalUnlock(h);}
  } finally {CloseClipboard();}
 }
 public bool RestoreIfUnchanged() {
  if(copied==0)return false;
  Open();
  try {
   if(GetClipboardSequenceNumber()!=copied)return false;
   if(!EmptyClipboard())throw new InvalidOperationException("clipboard_restore_failed");
   foreach(Saved item in saved){
    if(SetClipboardData(item.Format,item.Handle)==IntPtr.Zero)throw new InvalidOperationException("clipboard_restore_failed");
    item.Handle=IntPtr.Zero;
   }
   foreach(Saved item in saved) {
    IntPtr h=GetClipboardData(item.Format); IntPtr p=GlobalLock(h);
    if(p==IntPtr.Zero||GlobalSize(h).ToUInt64()<(ulong)item.Bytes.Length)throw new InvalidOperationException("clipboard_restore_failed");
    try {for(int i=0;i<item.Bytes.Length;i++)if(Marshal.ReadByte(p,i)!=item.Bytes[i])throw new InvalidOperationException("clipboard_restore_failed");}
    finally {GlobalUnlock(h);}
   }
   return true;
  } finally {CloseClipboard();}
 }
 public void Dispose(){
  foreach(Saved item in saved){
   if(item.Handle!=IntPtr.Zero){GlobalFree(item.Handle);item.Handle=IntPtr.Zero;}
   if(item.Bytes!=null)Array.Clear(item.Bytes,0,item.Bytes.Length);
  }
  if(owner!=IntPtr.Zero){DestroyWindow(owner);owner=IntPtr.Zero;}
 }
}
'''

CLIPBOARD_FUNCTIONS = r'''
function TaskMenuPopupElement($handle) {
 return [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$handle)
}
function TaskMenuRoots($window,$target) {
 $roots=@((ExactWindow $target))
 foreach($handle in [HermesClipboardApi]::TopLevelWindows()){
  if($handle -eq $target.window_id -or -not [HermesClipboardApi]::IsWindowVisible([IntPtr]$handle)){continue}
  [uint32]$processId=0
  [void][HermesClipboardApi]::GetWindowThreadProcessId([IntPtr]$handle,[ref]$processId)
  if($processId -ne $target.pid){continue}
  $owner=[HermesClipboardApi]::GetWindow([IntPtr]$handle,4);$owned=$false
  for($depth=0;$depth -lt 8 -and $owner -ne [IntPtr]::Zero;$depth++){
   if($owner.ToInt64() -eq $target.window_id){$owned=$true;break}
   $owner=[HermesClipboardApi]::GetWindow($owner,4)
  }
  if(-not $owned){continue}
  $popup=TaskMenuPopupElement $handle
  if($popup.Current.ProcessId -eq $target.pid){$roots+=$popup}
 }
 return $roots
}
function FindVisibleNamed($window,$name,$type,$menuTarget=$null) {
 $condition=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$name)
 $wait=[Diagnostics.Stopwatch]::StartNew()
 do {
  $roots=@($window)
  if($menuTarget -and $type -eq [System.Windows.Automation.ControlType]::MenuItem){$roots=@(TaskMenuRoots $window $menuTarget)}
  $matches=@{}
  foreach($root in $roots){
   foreach($element in $root.FindAll($desc,$condition)){
    if($element.Current.ControlType -eq $type -and -not $element.Current.IsOffscreen -and $element.Current.IsEnabled){
     $matches[(Rid $element)]=$element
    }
   }
  }
  if($matches.Count -eq 1){return @($matches.Values)[0]}
  if($matches.Count -gt 1){throw 'task_deeplink_ambiguous'}
  Start-Sleep -Milliseconds 50
 } while($wait.ElapsedMilliseconds -lt 1000)
 throw ('task_deeplink_unavailable_'+$name.Replace(' ','_'))
}
function CopyTaskDeeplink($window,$target) {
 $actions=$null;$copy=$null;$backup=$null;$answer=$null
 try {
  $button=FindVisibleNamed $window 'Chat actions' ([System.Windows.Automation.ControlType]::Button)
  $actions=$button.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
  if($actions.Current.ExpandCollapseState -ne [System.Windows.Automation.ExpandCollapseState]::Expanded){$actions.Expand();Start-Sleep -Milliseconds 150}
  $copyItem=FindVisibleNamed $window 'Copy' ([System.Windows.Automation.ControlType]::MenuItem) $target
  $copy=$copyItem.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
  if($copy.Current.ExpandCollapseState -ne [System.Windows.Automation.ExpandCollapseState]::Expanded){$copy.Expand();Start-Sleep -Milliseconds 150}
  $item=FindVisibleNamed $window 'Copy deeplink Alt+Ctrl+L' ([System.Windows.Automation.ControlType]::MenuItem) $target
  $invoke=$item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
  $backup=New-Object HermesClipboardBackup
  $backup.AssertUnchanged()
  [void](ExactWindow $target)
  $invoke.Invoke()
  $timer=[Diagnostics.Stopwatch]::StartNew()
  while($timer.ElapsedMilliseconds -lt 2000){
   $link=$backup.ReadCopy([uint32]$target.pid)
   if($null -ne $link){$answer=@{deeplink=$link;preserved_formats=$backup.FormatCount};break}
   Start-Sleep -Milliseconds 25
  }
  if($null -eq $answer){throw 'task_deeplink_unavailable'}
  return $answer
 } finally {
  if($backup){try {$restored=$backup.RestoreIfUnchanged();if($answer){$answer.clipboard_restored=$restored}}finally{$backup.Dispose()}}
  if($copy){try{if($copy.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Expanded){$copy.Collapse()}}catch{}}
  if($actions){try{if($actions.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Expanded){$actions.Collapse()}}catch{}}
 }
}
function CodexTaskBinding($window,$target) {
 $editor=FindVisibleNamed $window 'Do anything' ([System.Windows.Automation.ControlType]::Edit)
 $editorId=Rid $editor;$paneId=Rid ($walker.GetParent($editor))
 $copied=CopyTaskDeeplink $window $target
 $matched=[regex]::Match([string]$copied.deeplink,'^codex://threads/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$')
 if(-not $matched.Success){
  throw 'task_deeplink_unavailable'
 }
 $taskId=$matched.Groups[1].Value
 $fresh=ExactWindow $target
 $editor=FindVisibleNamed $fresh 'Do anything' ([System.Windows.Automation.ControlType]::Edit)
 if((Rid $editor) -cne $editorId -or (Rid ($walker.GetParent($editor))) -cne $paneId){throw 'recipient_task_changed'}
 if(($target.task_id -and $target.task_id -cne $taskId) -or
    ($target.composer_id -and $target.composer_id -cne $editorId) -or
    ($target.pane_id -and $target.pane_id -cne $paneId)){throw 'recipient_task_changed'}
 return @{task_id=$taskId;deeplink=$copied.deeplink;composer_id=$editorId;pane_id=$paneId;
  pid=$target.pid;window_id=$target.window_id;process_started=$target.process_started;
  clipboard_restored=$copied.clipboard_restored;preserved_formats=$copied.preserved_formats}
}

'''

CLIPBOARD_SCRIPT = ('Add-Type -TypeDefinition @"\n' + CLIPBOARD_NATIVE_API
                    + CLIPBOARD_IMPLEMENTATION + '\n"@\n' + CLIPBOARD_FUNCTIONS)
