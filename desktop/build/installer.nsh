; customUnInstall —— 卸载时把这套应用收干净：先收进程，再补删程序目录，最后按需清数据。
;
; 这套流程是踩坑踩出来的，几件事记在这（别回退）：
;   1. electron-builder 模板的卸载顺序是「先删 $INSTDIR → 删数据 → 最后才轮到本宏」，
;      它删文件时，采集/面板那串 python/powershell 子进程往往还活着（DETACHED 子进程
;      不随 Electron 退出），python.exe 被占用删不掉 —— 表现就是「卸载 exit=0，
;      程序目录和一堆文件还在」。
;   2. 杀进程必须用「脚本文件 + powershell -File」两步走：实测 nsExec 里直接
;      -Command "..."（含 \" 变体）在本环境 rc=1 什么都不执行（micro-test tmicro2）。
;      独立 .ps1 写进 $TEMP 再 -File 执行是唯一可靠写法（tmicro3 真杀假目标通过）。
;   3. 收进程的匹配 = 进程名限定 python/pythonw/powershell/winapp + 命令行含本应用脚本名
;      （assistant.py/server.py/watch.py/generate.py/wx_collector.py/wx_collect.ps1）
;      + 命令行含安装目录（$INSTDIR 带尾反斜杠；路径由 $TEMP\wx-uni-root.txt 传给
;      脚本，不走命令行参数，免得尾反斜杠被当成转义引号）。只按子串
;      太宽：编辑器打开项目里的文件、或用户另一个同名开发目录都会被误杀（审计 F-10 /
;      B7）。尾反斜杠不能省，否则 wx-reply-assistant-backup\ 会被前缀命中。
;      新版安装器叫 wx-reply-assistant-setup.exe，不在名单里，升级时不会被误杀。
;      连续两轮查到 0 才算干净，最多 30 轮（约 35 秒）。
;   4. 收完进程先 SetOutPath $TEMP 再 RMDir /r $INSTDIR 补一刀 —— 不换当前目录，
;      $INSTDIR 最后一层会因为「是工作目录」删不掉（留个空壳）。
;   5. 数据目录（%APPDATA%\wx-reply-assistant：聊天记录、面板密码、模型 Key）：
;        真卸载（无参数）                          → 删掉，卸载即无痕
;        升级覆盖装（--updated 或 /KEEP_APP_DATA） → 保留
;        显式 --delete-app-data                    → 无论如何都删
;      升级判断不能省：覆盖安装时新版安装器会先跑旧卸载器，误删就丢用户数据了。
;
; NSIS 转义（都是血泪）：FileWrite 行里 $ 写 $$、单引号原样；nsExec 里双引号原样。
; 构建开了 -WX，warning 6000 会直接打回；改完必须 rebuild 验证。

!macro customUnInstall
  ; ① 杀进程脚本写进 $TEMP（多行 FileWrite）
  FileOpen $9 "$TEMP\wx-uni-cleanup.ps1" w
  FileWrite $9 "$$me = $$PID$\r$\n"
  ; 安装目录从文件读，不走命令行：路径里若有尾反斜杠/空格/引号，命令行会被
  ; Windows 的引号转义规则吃掉（末尾 \" 会被当成转义引号），读文件没这些坑
  FileWrite $9 "$$root = ''$\r$\n"
  FileWrite $9 "$$rootFile = Join-Path $$env:TEMP 'wx-uni-root.txt'$\r$\n"
  FileWrite $9 "if(Test-Path $$rootFile){ $$root = (Get-Content $$rootFile -Raw).Trim() }$\r$\n"
  FileWrite $9 "if(-not $$root){ $$root = 'C:\__no_such_dir__' }$\r$\n"
  FileWrite $9 "$$rootl = ($$root.TrimEnd('\') + '\').ToLower()$\r$\n"
  FileWrite $9 "$$ns = @('python.exe','pythonw.exe','powershell.exe','winapp.exe')$\r$\n"
  FileWrite $9 "$$i = 0$\r$\n"
  FileWrite $9 "while($$i -lt 30){$\r$\n"
  FileWrite $9 "  $$p = @(Get-CimInstance Win32_Process | Where-Object { $$_.ProcessId -ne $$me -and $$ns -contains $$_.Name -and $$_.CommandLine -and $$_.CommandLine.ToLower().Contains($$rootl) -and $$_.CommandLine -match 'assistant\.py|server\.py|watch\.py|generate\.py|wx_collector\.py|wx_collect\.ps1' })$\r$\n"
  FileWrite $9 "  if($$p.Count -eq 0){$\r$\n"
  FileWrite $9 "    Start-Sleep -Milliseconds 1200$\r$\n"
  FileWrite $9 "    $$p = @(Get-CimInstance Win32_Process | Where-Object { $$_.ProcessId -ne $$me -and $$ns -contains $$_.Name -and $$_.CommandLine -and $$_.CommandLine.ToLower().Contains($$rootl) -and $$_.CommandLine -match 'assistant\.py|server\.py|watch\.py|generate\.py|wx_collector\.py|wx_collect\.ps1' })$\r$\n"
  FileWrite $9 "    if($$p.Count -eq 0){ break }$\r$\n"
  FileWrite $9 "  }$\r$\n"
  FileWrite $9 "  foreach($$q in $$p){ Stop-Process -Id $$q.ProcessId -Force -ErrorAction SilentlyContinue }$\r$\n"
  FileWrite $9 "  Start-Sleep -Milliseconds 900$\r$\n"
  FileWrite $9 "  $$i++$\r$\n"
  FileWrite $9 "}$\r$\n"
  FileClose $9
  ; ①' 安装目录单独写一个文件（同样用 FileWrite，避免 $INSTDIR 里的特殊字符）
  FileOpen $8 "$TEMP\wx-uni-root.txt" w
  FileWrite $8 "$INSTDIR"
  FileClose $8
  ; ② 执行：脚本自己读上面的文件拿 $INSTDIR，连续两轮干净才退出（最多约 35s）
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$TEMP\wx-uni-cleanup.ps1"'
  Pop $0
  Delete "$TEMP\wx-uni-cleanup.ps1"
  Delete "$TEMP\wx-uni-root.txt"
  Sleep 800
  ; ③ 换当前目录，补删程序目录（前面模板删过一轮，撞到被占文件的那部分这次补上）
  SetOutPath "$TEMP"
  RMDir /r "$INSTDIR"
  ; ④ 数据目录：真卸载才清；升级保留；--delete-app-data 强制清
  ClearErrors
  ${GetParameters} $R0
  ${GetOptions} $R0 "--delete-app-data" $R1
  ${If} ${Errors}
    ${GetOptions} $R0 "--updated" $R2
    ${If} ${Errors}
      ${GetOptions} $R0 "/KEEP_APP_DATA" $R3
      ${If} ${Errors}
        RMDir /r "$APPDATA\wx-reply-assistant"
        RMDir /r "$LOCALAPPDATA\wx-reply-assistant"
      ${EndIf}
    ${EndIf}
  ${Else}
    RMDir /r "$APPDATA\wx-reply-assistant"
    RMDir /r "$LOCALAPPDATA\wx-reply-assistant"
  ${EndIf}
!macroend
