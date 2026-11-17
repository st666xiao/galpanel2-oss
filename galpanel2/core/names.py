"""Conservative lexical naming; filesystem fallback belongs to scanning."""
import re
import unicodedata

EXCLUDED = frozenset("redist,_commonredist,commonredist,vcredist,directx,dxwebsetup,systemdata,setup,install,update,updates,patch,patches,汉化补丁,补丁,升级补丁,修正补丁,crack,破解,认证回避,save,savedata,存档,manual,マニュアル,特典,bonus,uninstall,$recycle.bin,system volume information,__macosx".split(","))
GENERIC = frozenset("bin,bins,game,win,win32,win64,x86,x64,system,exe,本体,実行,実行ファイル".split(","))
MEANINGLESS = frozenset("新建文件夹,新建文件夹 (2),新建文件夹 (3),new folder,下载,download,游戏,game,games,gal,galgame,解压,unzip,文件夹,folder,桌面,desktop,temp,tmp,未命名,untitled,杂项,empty,none,null,test".split(",")) | EXCLUDED | GENERIC
GROUP = r"\[[^\]]*\]|【[^】]*】|\([^)]*\)|（[^）]*）"


def parse(stem: str) -> tuple[str, str]:
    developer = ""
    text = stem.strip()
    first = re.match(GROUP, text)
    if first:
        developer = first.group()[1:-1]
        text = text[first.end():]
    text = re.sub(GROUP, "", text)
    text = re.sub(r"[\s_\-]*v(?:er)?\.?\d+(?:\.\d+)*[a-z]?$", "", text, flags=re.I)
    text = " ".join(text.replace("_", " ").split())
    return text, developer


def meaningful(text: str) -> bool:
    return len(text) > 2 and text.casefold() not in MEANINGLESS and \
        any(c.isalpha() for c in text)


def normalized_title(title: str) -> str:
    return "".join(unicodedata.normalize("NFKC", title).casefold().split())
