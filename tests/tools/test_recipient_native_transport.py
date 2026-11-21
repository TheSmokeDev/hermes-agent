"""The native subprocess waits for fresh authorization before any UI mutation."""
import io
import json

import pytest

from tools.computer_use.recipient_contract import RecipientError
from tools.computer_use.recipient_windows import WindowsRecipients


@pytest.mark.windows_only
@pytest.mark.parametrize("revoked", [False, True])
def test_native_ready_handshake_checks_authority_before_commit(monkeypatch, revoked):
    import tools.computer_use.recipient_windows as native
    from tools.computer_use import cua_backend

    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO('{"ready":true}\n')
            self.returncode = None
            self.committed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def poll(self):
            return self.returncode
        def communicate(self, value, timeout):
            assert authorized[0] and value == "commit\n"
            self.committed = True
            self.returncode = 0
            return '{"submitted":true}', ""
        def kill(self):
            self.returncode = -9
        def wait(self, timeout):
            return self.returncode

    process, authorized = Process(), [False]
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setattr(cua_backend, "sanitized_cua_driver_env", lambda: {})
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **kw: process)
    def authorize():
        payload = json.loads(process.stdin.getvalue().splitlines()[1])
        assert payload["action"] == "submit" and payload["target"]["composer_id"] == "exact-editor"
        if revoked:
            raise RecipientError("revoked", 403)
        authorized[0] = True
    desktop = WindowsRecipients(profiles={})
    if revoked:
        with pytest.raises(RecipientError, match="revoked"):
            desktop.submit({"composer_id": "exact-editor"}, "hello", "exact-send", authorize=authorize)
        assert not process.committed
    else:
        assert desktop.submit({"composer_id": "exact-editor"}, "hello", "exact-send",
                              authorize=authorize) == {"submitted": True}
        assert process.committed


@pytest.mark.windows_only
def test_null_native_control_types_have_no_action_role():
    import base64
    import os
    from pathlib import Path
    import subprocess
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.computer_use.recipient_windows_script import CONTROL_ROLE_SCRIPT
    command = CONTROL_ROLE_SCRIPT + """
$roles=@((ControlRole $null),(ControlRole ([pscustomobject]@{ProgrammaticName=$null})),(ControlRole ([pscustomobject]@{ProgrammaticName='ControlType.Edit'})))
ConvertTo-Json -InputObject $roles -Compress
"""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(command.encode("utf-16le")).decode()], capture_output=True,
                            text=True, encoding="utf-8", timeout=10, creationflags=windows_hide_flags())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["Unknown", "Unknown", "Edit"]


@pytest.mark.windows_only
def test_native_composer_pane_value_and_selectors_cover_both_codex_layouts():
    import base64
    import os
    import re
    from pathlib import Path
    import subprocess
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.computer_use.recipient_windows_clipboard import CLIPBOARD_FUNCTIONS
    from tools.computer_use.recipient_windows_script import CONTROL_ROLE_SCRIPT, SCRIPT
    composer_value = re.search(r"function ComposerValue\(.*?\n}\n", SCRIPT, re.S).group(0)
    script = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\n"
              "Add-Type -AssemblyName UIAutomationClient\nAdd-Type -AssemblyName UIAutomationTypes\n"
              + CONTROL_ROLE_SCRIPT + CLIPBOARD_FUNCTIONS + composer_value + r'''
$children='children';$any='any';$desc='desc';$request=$null
function Rid($element){return [string]$element.Id}
$T=[System.Windows.Automation.ControlType]
function Element($id,$name,$role,$parent){
 $e=[pscustomobject]@{Id=$id;Parent=$parent;Kids=@();Current=[pscustomobject]@{Name=$name;ControlType=$role;IsOffscreen=$false;IsEnabled=$true}}
 $e|Add-Member ScriptMethod FindAll {param($scope,$condition)
  if($scope -eq 'children'){return @($this.Kids)}
  $all=@();$queue=@($this.Kids)
  while($queue.Count){$head=$queue[0];$queue=@($queue|Select-Object -Skip 1)+@($head.Kids);$all+=$head}
  return @($all|Where-Object {$_.Current.ControlType.Id -eq $condition.Value})}
 if($parent){$parent.Kids+=$e}
 return $e
}
$walker=[pscustomobject]@{};$walker|Add-Member ScriptMethod GetParent {param($e);return $e.Parent}
function Layout($kind){
 $root=Element 'root' '' $T::Document $null
 if($kind -eq 'single'){
  $pane=Element 'pane' '' $T::Group $root;[void](Element 'mark' 'You said:' $T::Text $pane)
  return Element 'editor' 'Do anything' $T::Edit $pane
 }
 $conv=Element 'conversation' $(if($kind -eq 'unnamed_outer'){''}else{'Conversation'}) $T::Group $root
 [void](Element 'mark' 'You said:' $T::Text $conv)
 $box=Element 'box' $(if($kind -eq 'named_box'){'Composer'}else{''}) $T::Group $conv
 return Element 'editor' 'Work with Codex' $T::Edit $box
}
$panes=@{}
foreach($kind in @('single','conversation','named_box','unnamed_outer')){
 $layout=ComposerPane (Layout $kind);$panes[$kind]=@($layout.pane.Id,$layout.box.Id)
}
$request=[pscustomobject]@{selectors=[pscustomobject]@{conversation_names=@('Thread')}}
$layout=ComposerPane (Layout 'conversation');$panes['profile']=@($layout.pane.Id,$layout.box.Id)
$request=$null
function Composer($name,$raw,$parts){
 $c=Element 'c' $name $T::Edit $null
 foreach($p in $parts){[void](Element 'p' $p $T::Text $c)}
 return @{composer=$c;value=[pscustomobject]@{Current=[pscustomobject]@{Value=$raw}}}
}
$values=@{}
$cases=@{
 placeholder_last=@('Work with Codex',"Work with Codex`n",@('Work with Codex',"`n"));
 placeholder_first=@('Do anything',"`nDo anything",@("`n",'Do anything'));
 mismatched_order=@('Work with Codex',"Work with Codex`n",@("`n",'Work with Codex'));
 typed=@('Work with Codex','hello',@('hello'))
}
foreach($key in $cases.Keys){$c=Composer $cases[$key][0] $cases[$key][1] $cases[$key][2];$values[$key]=ComposerValue $c.composer $c.value}
$finds=@{}
foreach($kind in @('one','two','none')){
 $w=Element 'w' '' $T::Window $null
 if($kind -ne 'none'){[void](Element 'e1' 'Work with Codex' $T::Edit $w)}
 if($kind -eq 'two'){[void](Element 'e2' 'Do anything' $T::Edit $w)}
 [void](Element 'e3' 'Search' $T::Edit $w)
 try{$finds[$kind]=(FindVisibleComposer $w).Id}catch{$finds[$kind]=$_.Exception.Message}
}
@{panes=$panes;values=$values;finds=$finds}|ConvertTo-Json -Depth 4 -Compress
''')
    bootstrap = ("[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false);"
                 "Invoke-Expression ([Text.Encoding]::Unicode.GetString("
                 "[Convert]::FromBase64String([Console]::In.ReadLine())))")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
         base64.b64encode(bootstrap.encode("utf-16le")).decode()],
        input=base64.b64encode(script.encode("utf-16le")).decode() + "\n", capture_output=True,
        text=True, encoding="utf-8", timeout=30, creationflags=windows_hide_flags())
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["panes"] == {"single": ["pane", "pane"], "conversation": ["conversation", "box"],
                              "named_box": ["box", "box"], "unnamed_outer": ["box", "box"],
                              "profile": ["box", "box"]}
    assert value["values"] == {"placeholder_last": "", "placeholder_first": "",
                               "mismatched_order": "Work with Codex\n", "typed": "hello"}
    assert value["finds"] == {"one": "e1", "two": "task_deeplink_ambiguous",
                              "none": "task_deeplink_unavailable_composer"}


def test_codex_requests_carry_the_reviewed_selectors(monkeypatch):
    import tools.computer_use.recipient_windows as native
    from tools.computer_use import cua_backend
    import hermes_cli._subprocess_compat as compat
    sent = []

    class Result:
        stdout, returncode = '{"windows":[]}', 0

    def run(argv, input, **kwargs):
        sent.append(json.loads(input.splitlines()[1]))
        return Result()

    monkeypatch.setattr(native.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setattr(cua_backend, "sanitized_cua_driver_env", lambda: {})
    monkeypatch.setattr(compat, "windows_hide_flags", lambda: 0)
    monkeypatch.setattr(native.subprocess, "run", run)
    WindowsRecipients(profiles={})._call("snapshot", target={"pid": 1})
    WindowsRecipients(profiles={"codex_desktop": {"composer_names": ["Ask Codex"]}})._call("snapshot", target={})
    WindowsRecipients(profiles={})._call("windows")
    assert sent[0]["selectors"] == {"composer_names": ["Do anything", "Work with Codex"],
                                    "user_markers": ["You said:"], "conversation_names": ["Conversation"]}
    assert sent[1]["selectors"]["composer_names"] == ["Ask Codex"]
    assert "selectors" not in sent[2]
