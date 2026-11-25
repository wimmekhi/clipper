"""1.3.4 采集包：文件浏览器式的树（多级文件夹）、包内片段列表、收藏到选择框、新建并收藏确认框。

收藏规则（硬性）：收藏目标包 = 当前搜索的句型。同名（Library.norm_name：大小写、空白、... ≡ …）的包存在就收进去，
有好几个（在不同文件夹）就用最近用过的；不存在就弹 NewPackDialog 确认新建；不能收进名字不同的包。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF, QSize, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QFontMetrics, QPalette
from PySide6.QtWidgets import (QTreeWidget, QTreeWidgetItem, QAbstractItemView, QStyledItemDelegate, QStyle,
                               QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QLabel, QDialog, QDialogButtonBox, QHeaderView)

from . import theme
from .theme import icon

SEP = " › "
K_FOLDER, K_PACK = "f", "p"
ROLE = Qt.UserRole


def path_text(parts, max_px=None, fm: QFontMetrics | None = None) -> str:
    """路径显示成「NCE1 › L1-L2 › 包名」；太长就中间省略（保留开头和包名）。"""
    s = SEP.join(parts)
    if max_px and fm is not None and fm.horizontalAdvance(s) > max_px:
        s = fm.elidedText(s, Qt.ElideMiddle, int(max_px))
    return s


def soft_highlight(view):
    """选中行的高亮（含左边的缩进 / 箭头区）用浅青色，不用整块深青。"""
    pal = view.palette()
    for g in (QPalette.Active, QPalette.Inactive):
        pal.setColor(g, QPalette.Highlight, QColor(204, 251, 241))
        pal.setColor(g, QPalette.HighlightedText, QColor(15, 23, 42))
    view.setPalette(pal)


# ------------------------------------------------------------------ 树
class PackTree(QTreeWidget):
    """左边的树：文件夹（显示子包个数）/ 采集包（显示条数）。拖动换位置。"""
    moved = Signal(str, int, object, object)      # kind, id, 新父文件夹 id(None=最外层), 同类里的位置

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setIndentation(18)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setObjectName("packtree")
        self.setStyleSheet("QTreeWidget#packtree{background:transparent;border:none;}"
                           "QTreeWidget#packtree::item{height:26px;border:none;}"
                           "QTreeWidget#packtree::item:selected{background:rgba(45,212,191,0.22);color:#0F172A;}"
                           "QTreeWidget#packtree::item:hover:!selected{background:rgba(148,163,184,0.12);}"
                           "QTreeWidget#packtree::branch:selected{background:transparent;}")
        soft_highlight(self)
        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.ic_folder = icon("folder", "#E0A100", 16, fill="#FCD34D")
        self.ic_pack = icon("package", theme.ACCENT_D, 16)
        self.items: dict = {}          # (kind, id) -> item

    def build(self, folders, packs, expanded: set | None = None):
        """整棵树重建（上千个包也快：先建好再一次挂上去）。"""
        expanded = expanded if expanded is not None else self.expanded_ids()
        self.setUpdatesEnabled(False)
        self.clear()
        self.items = {}
        kids: dict = {}
        for f in folders:
            kids.setdefault(f["parent_id"], []).append(f)
        pk: dict = {}
        for p in packs:
            pk.setdefault(p["folder_id"], []).append(p)
        counts: dict = {}

        def count(fid):            # 文件夹下（递归）的包数
            if fid in counts:
                return counts[fid]
            n = len(pk.get(fid, [])) + sum(count(f["id"]) for f in kids.get(fid, []))
            counts[fid] = n
            return n

        def make(fid):
            out = []
            for f in kids.get(fid, []):
                it = QTreeWidgetItem([f["name"], f"{count(f['id'])} 包"])
                it.setIcon(0, self.ic_folder)
                it.setData(0, ROLE, (K_FOLDER, f["id"]))
                it.setFlags(it.flags() | Qt.ItemIsDropEnabled | Qt.ItemIsDragEnabled)
                it.setForeground(1, QColor(theme.TEXT2))
                it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                it.addChildren(make(f["id"]))
                self.items[(K_FOLDER, f["id"])] = it
                out.append(it)
            for p in pk.get(fid, []):
                it = QTreeWidgetItem([p["name"], str(p["n"])])
                it.setIcon(0, self.ic_pack)
                it.setData(0, ROLE, (K_PACK, p["id"]))
                it.setFlags((it.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)
                it.setForeground(1, QColor(theme.TEXT2))
                it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                if fid is None:
                    it.setToolTip(0, f"未分类：{p['name']}（拖进文件夹即可归类）")
                self.items[(K_PACK, p["id"])] = it
                out.append(it)
            return out
        self.addTopLevelItems(make(None))
        for k in expanded:
            it = self.items.get((K_FOLDER, k))
            if it is not None:
                it.setExpanded(True)
        self.setUpdatesEnabled(True)

    def expanded_ids(self) -> set:
        return {k[1] for k, it in self.items.items() if k[0] == K_FOLDER and it.isExpanded()}

    def select(self, kind, id_):
        it = self.items.get((kind, id_))
        if it is None:
            return False
        p = it.parent()
        while p is not None:
            p.setExpanded(True); p = p.parent()
        self.setCurrentItem(it)
        self.scrollToItem(it)
        return True

    def current(self):
        it = self.currentItem()
        return it.data(0, ROLE) if it is not None else None

    def apply_filter(self, text: str):
        """输入即筛选：命中的包 / 文件夹连同祖先文件夹显示并展开。"""
        t = text.strip().lower()
        self.setUpdatesEnabled(False)

        def walk(it) -> bool:
            hit = bool(t) and t in it.text(0).lower()
            any_child = False
            for i in range(it.childCount()):
                any_child = walk(it.child(i)) or any_child
            show = (not t) or hit or any_child
            it.setHidden(not show)
            if t and any_child:
                it.setExpanded(True)
            return hit or any_child
        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        self.setUpdatesEnabled(True)

    # 拖动：自己算目标（文件夹里 / 某项前后），交给库挪，再整棵重建
    def dropEvent(self, e):
        src = self.currentItem()
        if src is None:
            e.ignore(); return
        kind, sid = src.data(0, ROLE)
        tgt = self.itemAt(e.position().toPoint())
        pos = self.dropIndicatorPosition()
        if tgt is None:
            parent, index = None, None
        else:
            tk, tid = tgt.data(0, ROLE)
            if pos == QAbstractItemView.OnItem and tk == K_FOLDER:
                parent, index = tid, None
            else:
                par = tgt.parent()
                parent = par.data(0, ROLE)[1] if par is not None else None
                sibs = [par.child(i) for i in range(par.childCount())] if par is not None else \
                    [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
                same = [s for s in sibs if s.data(0, ROLE)[0] == kind and s is not src]
                if tgt in same:
                    index = same.index(tgt) + (1 if pos == QAbstractItemView.BelowItem else 0)
                else:
                    index = 0 if kind == K_PACK else None
        e.setDropAction(Qt.IgnoreAction)
        e.accept()
        self.moved.emit(kind, sid, parent, index)


# ------------------------------------------------------------------ 包里的片段
class PackItemDelegate(QStyledItemDelegate):
    """一条片段：序号 / 英文 / 中文 ……  出处标签  时长  ▶ 剪辑"""
    H = 62

    def sizeHint(self, opt, idx):
        return QSize(opt.rect.width(), self.H)

    def paint(self, p, opt, idx):
        d = idx.data(ROLE + 2) or {}
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(4, 3, -4, -3)
        sel = bool(opt.state & QStyle.State_Selected)
        p.setPen(QPen(QColor(theme.ACCENT) if sel else QColor(226, 232, 240), 1.2))
        p.setBrush(QColor(204, 251, 241, 150) if sel else QColor(255, 255, 255, 220))
        p.drawRoundedRect(r, 9, 9)
        f = QFont(opt.font); f.setPointSizeF(max(9.0, f.pointSizeF()))
        fb = QFont(f); fb.setPointSizeF(f.pointSizeF() + 1.5); fb.setWeight(QFont.Medium)
        p.setFont(f); p.setPen(QColor(theme.TEXT2))
        p.drawText(QRectF(r.left() + 10, r.top() + 6, 30, 22), Qt.AlignLeft | Qt.AlignVCenter, f"{idx.row() + 1}.")
        fm = QFontMetrics(f)
        src = d.get("source", "")
        tw = fm.horizontalAdvance(src) + 16
        x_play = r.right() - 66
        x_dur = x_play - 62
        xr = x_dur - 8 - tw
        p.setPen(Qt.NoPen); p.setBrush(QColor(20, 184, 166, 30))
        p.drawRoundedRect(QRectF(xr, r.top() + 8, tw, 20), 7, 7)
        p.setPen(QColor(theme.ACCENT_D))
        p.drawText(QRectF(xr, r.top() + 8, tw, 20), Qt.AlignCenter, src)
        p.setPen(QColor(theme.TEXT2))
        p.drawText(QRectF(x_dur, r.top() + 8, 58, 20), Qt.AlignRight | Qt.AlignVCenter, d.get("dur", ""))
        p.setPen(QColor(theme.ACCENT_D))
        p.drawText(QRectF(x_play + 4, r.top() + 8, 60, 20), Qt.AlignLeft | Qt.AlignVCenter, "▶ 剪辑")
        textw = max(40, xr - (r.left() + 40) - 10)
        p.setFont(fb); p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(r.left() + 40, r.top() + 5, textw, 24), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(fb).elidedText(d.get("en", ""), Qt.ElideRight, int(textw)))
        if d.get("zh"):
            p.setFont(f); p.setPen(QColor(theme.TEXT2))
            p.drawText(QRectF(r.left() + 40, r.top() + 30, textw, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(d["zh"], Qt.ElideRight, int(textw)))
        p.restore()


class PackItemList(QListWidget):
    """包里的片段：拖动排序（松手后 reordered 发出新顺序）。"""
    reordered = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(PackItemDelegate(self))
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setUniformItemSizes(True)
        self.setSpacing(1)
        self.setObjectName("packitems")

    def dropEvent(self, e):
        super().dropEvent(e)
        self.reordered.emit([self.item(i).data(ROLE) for i in range(self.count())])


# ------------------------------------------------------------------ 收藏到：选择框
class PackPicker(QFrame):
    """点「收藏到：…」弹出：只列和当前句型同名的包（不同文件夹），最近用过的在上面；
    输入筛选，回车选第一个；底部「＋ 新建到其他文件夹…」。"""
    picked = Signal(int)
    create = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("panel")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        v = QVBoxLayout(self); v.setContentsMargins(10, 10, 10, 10); v.setSpacing(6)
        self.title = QLabel(""); self.title.setObjectName("muted"); self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.ed = QLineEdit(); self.ed.setPlaceholderText("筛选文件夹…")
        self.ed.textChanged.connect(self._filter)
        self.ed.returnPressed.connect(self._enter)
        v.addWidget(self.ed)
        self.lst = QListWidget(); self.lst.setObjectName("script")
        self.lst.itemActivated.connect(self._act)
        self.lst.itemClicked.connect(self._act)
        v.addWidget(self.lst, 1)
        self.b_new = QPushButton("＋ 新建到其他文件夹…"); self.b_new.setObjectName("ghost")
        self.b_new.clicked.connect(lambda: (self.hide(), self.create.emit()))
        v.addWidget(self.b_new)
        self.resize(380, 300)

    def fill(self, pattern: str, entries, current):
        """entries: [(包 id, 路径列表, 条数, 是否最近用过)]"""
        self.title.setText(f"收藏到和句型「{pattern}」同名的采集包：" if entries else
                           f"还没有采集包「{pattern}」")
        self.lst.clear()
        for pid, path, n, recent in entries:
            it = QListWidgetItem(("★ " if recent else "　 ") + SEP.join(path) + f"　{n} 条")
            it.setData(ROLE, pid)
            it.setToolTip(SEP.join(path))
            if pid == current:
                f = it.font(); f.setBold(True); it.setFont(f)
            self.lst.addItem(it)
        self.b_new.setText("＋ 新建到其他文件夹…" if entries else f"＋ 新建采集包「{pattern}」…")
        self.ed.clear()
        if self.lst.count():
            self.lst.setCurrentRow(0)

    def _filter(self, t):
        t = t.strip().lower()
        first = None
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            hide = bool(t) and t not in it.text().lower()
            it.setHidden(hide)
            if not hide and first is None:
                first = i
        if first is not None:
            self.lst.setCurrentRow(first)

    def _enter(self):
        it = self.lst.currentItem()
        if it is not None and not it.isHidden():
            self._act(it)
        elif not any(not self.lst.item(i).isHidden() for i in range(self.lst.count())):
            self.hide(); self.create.emit()

    def _act(self, it):
        self.hide()
        self.picked.emit(it.data(ROLE))

    def popup_at(self, w):
        g = w.mapToGlobal(w.rect().bottomLeft())
        self.move(g.x(), g.y() + 4)
        self.show()
        self.ed.setFocus()


# ------------------------------------------------------------------ 新建并收藏
class NewPackDialog(QDialog):
    """还没有和句型同名的包：确认新建并收藏；可以改选放进哪个文件夹。"""

    def __init__(self, lib, name: str, default_folder=None, parent=None, title="新建并收藏"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.lib = lib
        v = QVBoxLayout(self)
        head = QLabel(f"还没有采集包「{name}」，新建并收藏？" if title == "新建并收藏"
                      else f"把采集包「{name}」新建在哪个文件夹？")
        head.setWordWrap(True)
        f = head.font(); f.setPointSizeF(f.pointSizeF() + 1.5); f.setBold(True); head.setFont(f)
        v.addWidget(head)
        self.where = QLabel(""); self.where.setObjectName("muted")
        v.addWidget(self.where)
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True); self.tree.setIndentation(16)
        soft_highlight(self.tree)
        root = QTreeWidgetItem(["最外层（未分类）"]); root.setData(0, ROLE, None)
        root.setIcon(0, icon("package", theme.ACCENT_D, 16))
        self.tree.addTopLevelItem(root)
        kids: dict = {}
        for fo in lib.folders():
            kids.setdefault(fo["parent_id"], []).append(fo)
        self._items = {None: root}
        ic = icon("folder", "#E0A100", 16, fill="#FCD34D")

        def add(par_item, pid):
            for fo in kids.get(pid, []):
                it = QTreeWidgetItem([fo["name"]]); it.setData(0, ROLE, fo["id"]); it.setIcon(0, ic)
                par_item.addChild(it)
                self._items[fo["id"]] = it
                add(it, fo["id"])
        add(root, None)
        root.setExpanded(True)
        self.tree.currentItemChanged.connect(self._sync)
        v.addWidget(self.tree, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("新建并收藏" if title == "新建并收藏" else "新建")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)
        it = self._items.get(default_folder, root)
        p = it.parent()
        while p is not None:
            p.setExpanded(True); p = p.parent()
        self.tree.setCurrentItem(it)
        self._sync()
        self.resize(440, 380)

    def folder_id(self):
        it = self.tree.currentItem()
        return it.data(0, ROLE) if it is not None else None

    def set_folder(self, fid):
        it = self._items.get(fid)
        if it is not None:
            self.tree.setCurrentItem(it)

    def _sync(self, *_):
        fid = self.folder_id()
        self.where.setText("放进：" + (SEP.join(self.lib.folder_path(fid)) if fid is not None else "最外层（未分类）"))
