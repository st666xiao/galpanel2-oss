"""Offline brand and tag management, with all database work on Window jobs."""
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QLineEdit, QMenu, QInputDialog, QMessageBox)
from galpanel2.core import meta as M
from .theme import font
from .widgets import Button, Adv, rounded


def metadata_write(owner, label, fn, done=None, failed=None):
    if not owner.lib or owner.lib.readonly or owner.busy:return
    lib=owner.lib
    def completed(value):
        owner.reload_library()
        if done:done(value)
    on_error=failed or (lambda error:owner.notify(str(error)))
    try:
        owner.run_job(label,lambda cancel:fn(lib),completed,on_error,
                      cancellable=False,writes_library=True)
    except Exception as error:on_error(error)


def rename_item(owner,kind,item,done=None,failed=None):
    value,ok=QInputDialog.getText(owner,'改名','名字',text=item.name)
    if ok:metadata_write(owner,'改名',lambda lib:(M.rename_brand if kind=='brand' else M.rename_tag)(lib,item.id,value),done,failed)


def merge_item(owner,kind,item,done=None,failed=None):
    items=owner.library_page.brands if kind=='brand' else owner.library_page.tags
    others=[x for x in items.values() if x.id!=item.id]
    if not others:return
    value,ok=QInputDialog.getItem(owner,'合并到','目标品牌' if kind=='brand' else '目标标签',[x.name for x in others],0,False)
    if not ok:return
    target=next(x for x in others if x.name==value)
    message=(f'把「{item.name}」的 {item.work_count} 部作品并到「{target.name}」，{item.name} 的名字变成 {target.name} 的别名。'
             if kind=='brand' else f'把「{item.name}」的 {item.work_count} 部作品并到「{target.name}」，然后删除「{item.name}」。')
    if QMessageBox.question(owner,'确认合并',message)!=QMessageBox.StandardButton.Yes:return
    metadata_write(owner,'合并品牌' if kind=='brand' else '合并标签',
                   lambda lib:(M.merge_brands if kind=='brand' else M.merge_tags)(lib,item.id,target.id),done,failed)


def delete_item(owner,kind,item,done=None,failed=None):
    if kind=='brand' and item.work_count:return
    message=f'从 {item.work_count} 部作品上去掉「{item.name}」，然后删除这个标签。' if kind=='tag' else f'删除没有作品的品牌「{item.name}」？'
    if QMessageBox.question(owner,'确认删除',message)!=QMessageBox.StandardButton.Yes:return
    metadata_write(owner,'删除品牌' if kind=='brand' else '删除标签',
                   lambda lib:(M.delete_brand if kind=='brand' else M.delete_tag)(lib,item.id),done,failed)


def metadata_menu(owner,kind,item,pos):
    if not owner.lib or owner.lib.readonly or owner.busy:return
    menu=QMenu(owner)
    menu.addAction('改名…',lambda:rename_item(owner,kind,item))
    if kind=='brand':
        def aliases():
            value,ok=QInputDialog.getMultiLineText(owner,'编辑别名','一行一个','\n'.join(item.aliases))
            if ok:metadata_write(owner,'编辑别名',lambda lib:M.set_brand_aliases(lib,item.id,[s for s in value.splitlines() if s.strip()]))
        menu.addAction('编辑别名…',aliases)
    else:
        sub=menu.addMenu('改分组')
        for g in owner.library_page.groups:
            action=sub.addAction(g.name,lambda checked=False,i=g.id:metadata_write(owner,'改标签分组',lambda lib:M.set_tag_group(lib,item.id,i)))
            action.setCheckable(True);action.setChecked(g.id==item.group_id)
    menu.addAction('合并到…',lambda:merge_item(owner,kind,item))
    action=menu.addAction('删除' if kind=='brand' else '删除…',lambda:delete_item(owner,kind,item))
    action.setEnabled(kind=='tag' or item.work_count==0)
    menu.exec(pos)


class MetaDialog(QDialog):
    def __init__(self,owner):
        super().__init__(owner)
        self.owner=owner;self.saving=False;self.loading=False
        self.lib=owner.lib
        if owner.screenshot_mode:self.setWindowFlags(Qt.WindowType.Widget)
        else:self.setModal(True)
        self.layout_box=QVBoxLayout(self);self.layout_box.setContentsMargins(24,20,24,20);self.layout_box.setSpacing(10)
        self.message=QLabel();self.message.setWordWrap(True)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t=self.owner.theme
        rounded(p,QRectF(self.rect()).adjusted(1,1,-1,-1),t.color('card'),18,t.color('white' if t.name=='day' else 'line'),2)
        rounded(p,QRectF(self.rect()).adjusted(6,6,-6,-6),t.color('card',0),14,t.color('sakura'))

    def reject(self):
        if not self.saving:super().reject()

    def closeEvent(self,event):
        if self.saving:event.ignore()
        else:super().closeEvent(event)

    def present(self):
        if self.owner.screenshot_mode:
            self.move((self.owner.width()-self.width())//2,(self.owner.height()-self.height())//2)
        self.show();self.raise_()


class BrandTidyDialog(MetaDialog):
    def __init__(self,owner):
        super().__init__(owner)
        self.setWindowTitle('整理品牌');self.resize(1040,740)
        self.texts=[];self.brands={};self.choices={};self.widgets={}
        header=QHBoxLayout()
        label=QLabel('整理品牌');label.setFont(font('title',18,True));header.addWidget(label)
        label=QLabel('FROM OLD DEVELOPER TEXT');label.setFont(font('body',9,True));label.setStyleSheet(f"color:{owner.theme.roles['beni']}")
        header.addWidget(label);header.addStretch();self.layout_box.addLayout(header)
        self.only_pending=QCheckBox('只看还没处理的');self.only_pending.setChecked(True)
        self.only_pending.toggled.connect(self.rebuild);self.layout_box.addWidget(self.only_pending)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['旧社团文字','作品','怎么处理','品牌名','说明'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.verticalHeader().hide()
        for col,width in ((0,235),(1,50),(2,105),(3,235)):self.table.setColumnWidth(col,width)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.Stretch)
        self.layout_box.addWidget(self.table,1)
        self.summary=QLabel();self.summary.setFont(font('body',11));self.layout_box.addWidget(self.summary)
        self.layout_box.addWidget(self.message)
        buttons=QHBoxLayout();buttons.addStretch()
        buttons.addWidget(Button(owner,'关闭',self.reject))
        self.apply_button=Button(owner,'应用 0 条',self.apply,primary=True,subtitle='APPLY')
        self.apply_button.setEnabled(False);buttons.addWidget(self.apply_button);self.layout_box.addLayout(buttons)
        self.adv=Adv(owner,pose='stamp',height=245)
        self.adv.text='旧库的社团写法乱七八糟的……我先按出现次数排好了，每一行你说了算。'
        self.adv.subtitle='—— 点「应用」才会一次写进库，之前随便改。'
        self.adv.setFixedWidth(600)
        self.layout_box.addWidget(self.adv,alignment=Qt.AlignmentFlag.AlignRight)

    def reload(self):
        if self.owner.busy:return
        self.loading=True;self.apply_button.setEnabled(False);lib=self.owner.lib
        try:self.owner.run_job('读取旧社团文字',lambda cancel:(M.developer_texts(lib),M.list_brands(lib)),self.loaded,self.failed,cancellable=False)
        except Exception as error:self.failed(error)

    def loaded(self,data):
        self.loading=False;self.texts,brands=data;self.brands={b.id:b for b in brands};self.choices={}
        for t in self.texts:
            action='merge' if t.decided=='brand' else 'ignore' if t.decided=='ignore' else t.suggestion
            self.choices[t.text_key]=M.DevDecision(t.text_key,action,t.cleaned if action=='new' else None,
                                                 (t.decided_brand_id if t.decided else t.brand_id) if action=='merge' else None)
        self.rebuild()

    def rebuild(self,*args):
        self.table.setRowCount(0);self.widgets={}
        for t in self.texts:
            if self.only_pending.isChecked() and t.decided is not None:continue
            row=self.table.rowCount();self.table.insertRow(row);self.table.setRowHeight(row,42)
            self.table.setItem(row,0,QTableWidgetItem(t.text));self.table.setItem(row,1,QTableWidgetItem(str(len(t.work_ids))))
            action=QComboBox()
            for label,key in (('新建','new'),('并入','merge'),('忽略','ignore')):action.addItem(label,key)
            choice=self.choices[t.text_key];action.setCurrentIndex(action.findData(choice.action))
            self.table.setCellWidget(row,2,action)
            self.widgets[t.text_key]=(row,action)
            self.target_widget(t)
            action.currentIndexChanged.connect(lambda idx,text=t:self.action_changed(text))
        self.update_summary()

    def action_changed(self,t):
        _,combo=self.widgets[t.text_key];action=combo.currentData();old=self.choices[t.text_key]
        target=old.brand_id or t.brand_id or next(iter(self.brands),None)
        self.choices[t.text_key]=M.DevDecision(t.text_key,action,old.brand_name or t.cleaned if action=='new' else None,
                                            target if action=='merge' else None)
        self.target_widget(t);self.update_summary()

    def target_widget(self,t):
        row,combo=self.widgets[t.text_key];d=self.choices[t.text_key]
        if d.action=='new':
            field=QLineEdit(d.brand_name or '')
            field.textChanged.connect(lambda value,key=t.text_key:self.new_name(key,value))
        elif d.action=='merge':
            field=QComboBox();field.setEditable(True);field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            field.completer().setFilterMode(Qt.MatchFlag.MatchContains)
            for b in self.brands.values():field.addItem(b.name,b.id)
            field.setCurrentIndex(field.findData(d.brand_id))
            field.currentIndexChanged.connect(lambda idx,key=t.text_key,f=field:self.merge_target(key,f.currentData()))
            field.editTextChanged.connect(lambda value,key=t.text_key:self.merge_target(key,self.brand_for_text(value)))
        else:
            field=QLabel('忽略（不挂品牌）');field.setStyleSheet(f"color:{self.owner.theme.roles['sub']}")
        self.table.setCellWidget(row,3,field)
        self.update_explanation(t)

    def new_name(self,key,name):
        self.choices[key]=M.DevDecision(key,'new',name);self.update_summary()

    def brand_for_text(self,value):
        key=M.name_key(value)
        if not key:return None
        for b in self.brands.values():
            if M.name_key(b.name)==key:return b.id
        for b in self.brands.values():
            if key in {M.name_key(a) for a in b.aliases}:return b.id
        return None

    def valid(self,d):
        return d.action!='merge' or (d.brand_id is not None and d.brand_id in self.brands)

    def invalid_count(self):
        return sum(not self.valid(d) for d in self.rows())

    def merge_target(self,key,item_id):
        self.choices[key]=M.DevDecision(key,'merge',brand_id=item_id)
        self.update_explanation(next(t for t in self.texts if t.text_key==key));self.update_summary()

    def update_explanation(self,t):
        if t.text_key not in self.widgets:return
        d=self.choices[t.text_key];notes=[]
        if not self.valid(d):notes.append('请从列表里选一个品牌')
        if t.cleaned!=t.text:notes.append('去掉了残缺括号')
        b=self.brands.get(d.brand_id)
        if d.action=='merge' and b and M.name_key(t.cleaned) not in {M.name_key(n) for n in (b.name,*b.aliases)} and t.cleaned:
            notes.append('会存成别名')
        if t.suggestion=='ignore':notes.append('像平台名或标记')
        item=QTableWidgetItem('；'.join(notes));item.setForeground(self.owner.theme.color('sub'))
        self.table.setItem(self.widgets[t.text_key][0],4,item)

    def rows(self):
        return [self.choices[t.text_key] for t in self.texts if t.text_key in self.widgets]

    def set_row(self,text_key,action,brand_name=None,brand_id=None):
        if text_key not in self.choices:raise KeyError(text_key)
        if action not in ('new','merge','ignore'):raise ValueError('Unknown developer decision')
        self.choices[text_key]=M.DevDecision(text_key,action,brand_name if action=='new' else None,brand_id if action=='merge' else None)
        self.rebuild()

    def update_summary(self):
        rows=self.rows();counts={a:sum(d.action==a for d in rows) for a in ('new','merge','ignore')}
        self.summary.setText(f'新建 {counts["new"]} · 并入 {counts["merge"]} · 忽略 {counts["ignore"]}　·　忽略的留在「无品牌」，旧文字不删')
        invalid=self.invalid_count()
        if invalid:self.summary.setText(self.summary.text()+f'　·　有 {invalid} 行「并入」还没选品牌')
        elif self.message.text().startswith('有 '):self.message.setText('')
        self.apply_button.setText(f'应用 {len(rows)} 条')
        self.apply_button.setEnabled(bool(rows) and not invalid and not self.saving and not self.loading and bool(self.owner.lib and not self.owner.lib.readonly))

    def apply(self):
        if self.saving or self.loading or self.owner.busy or not self.owner.lib or self.owner.lib.readonly or not self.rows():return
        if self.owner.lib is not self.lib:self.message.setText('库已切换，请重新打开整理品牌');return
        invalid=self.invalid_count()
        if invalid:self.message.setText(f'有 {invalid} 行「并入」还没选品牌');return
        decisions=self.rows();lib=self.owner.lib;self.saving=True;self.update_summary();self.table.setEnabled(False)
        try:
            self.owner.run_job('整理品牌',lambda cancel:M.apply_developer_decisions(lib,decisions),self.applied,self.failed,
                               cancellable=False,writes_library=True)
        except Exception as error:self.failed(error)

    def applied(self,result):
        from .library import load_projection
        self.saving=False;self.table.setEnabled(True)
        message=f'新建 {result.created_brands} 个品牌，{result.linked_works} 部作品挂上了品牌'
        self.message.setText(message);self.loading=True;lib=self.owner.lib
        def read(cancel):return load_projection(lib,cancel),M.developer_texts(lib),M.list_brands(lib)
        def loaded(data):
            projection,texts,brands=data
            self.owner.library_page.set_data(projection)
            self.owner.source_page.set_data(projection[3],projection[1])
            self.loaded((texts,brands));self.owner.last_message=message;self.owner.status.idle(message)
        self.owner.run_job('刷新品牌与书架',read,loaded,self.failed,cancellable=False)

    def failed(self,error):
        self.saving=self.loading=False;self.table.setEnabled(True);self.message.setText(str(error));self.update_summary()


class TagManagerDialog(MetaDialog):
    def __init__(self,owner):
        super().__init__(owner)
        self.setWindowTitle('整理标签');self.resize(800,520)
        self.tags=[];self.groups=[]
        title=QLabel('整理标签');title.setFont(font('title',18,True));self.layout_box.addWidget(title)
        row=QHBoxLayout();row.addWidget(QLabel('新建标签'))
        self.name_input=QLineEdit();row.addWidget(self.name_input)
        self.group_input=QComboBox();row.addWidget(self.group_input)
        self.create_button=Button(owner,'新建',self.create,primary=True);row.addWidget(self.create_button);self.layout_box.addLayout(row)
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['标签','分组','作品','操作']);self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1,115);self.table.setColumnWidth(2,55);self.table.setColumnWidth(3,280)
        self.layout_box.addWidget(self.table,1);self.layout_box.addWidget(self.message)
        self.layout_box.addWidget(Button(owner,'关闭',self.reject),alignment=Qt.AlignmentFlag.AlignRight)

    def reload(self):
        if self.owner.busy:return
        lib=self.owner.lib
        try:self.owner.run_job('读取标签',lambda cancel:(M.list_tags(lib),M.list_tag_groups(lib)),self.loaded,self.failed,cancellable=False)
        except Exception as error:self.failed(error)

    def loaded(self,data):
        self.tags,self.groups=data;self.table.setRowCount(0)
        selected=self.group_input.currentData();self.group_input.clear()
        for g in self.groups:self.group_input.addItem(g.name,g.id)
        if selected:self.group_input.setCurrentIndex(self.group_input.findData(selected))
        readonly=self.owner.lib.readonly
        self.create_button.setEnabled(not readonly);self.name_input.setEnabled(not readonly);self.group_input.setEnabled(not readonly)
        for t in self.tags:
            row=self.table.rowCount();self.table.insertRow(row);self.table.setRowHeight(row,40)
            self.table.setItem(row,0,QTableWidgetItem(t.name));self.table.setItem(row,2,QTableWidgetItem(str(t.work_count)))
            group=QComboBox()
            for g in self.groups:group.addItem(g.name,g.id)
            group.setCurrentIndex(group.findData(t.group_id));group.setEnabled(not readonly)
            group.currentIndexChanged.connect(lambda idx,item=t,combo=group:self.change_group(item.id,combo.currentData()))
            self.table.setCellWidget(row,1,group)
            holder=QWidget();actions=QHBoxLayout(holder);actions.setContentsMargins(2,0,2,0)
            for text,fn in [('改名…',rename_item),('合并到…',merge_item),('删除…',delete_item)]:
                button=Button(self.owner,text,lambda checked=False,item=t,f=fn:self.action(f,item));button.setEnabled(not readonly);actions.addWidget(button)
            self.table.setCellWidget(row,3,holder)

    def action(self,fn,item):
        # Shared operations reload the shelf; queue manager refresh after that read finishes.
        fn(self.owner,'tag',item,lambda value:self.defer_reload(),self.failed)

    def defer_reload(self):
        if self.owner.closing:return
        if self.owner.busy:QTimer.singleShot(20,self.defer_reload)
        else:self.reload()

    def create(self):
        name,group=self.name_input.text(),self.group_input.currentData()
        self.write('新建标签',lambda lib:M.create_tag(lib,name,group))

    def change_group(self,tag_id,group_id):
        self.write('改标签分组',lambda lib:M.set_tag_group(lib,tag_id,group_id))

    def write(self,label,fn):
        if self.owner.busy or self.owner.lib.readonly:return
        if self.owner.lib is not self.lib:self.message.setText('库已切换，请重新打开整理标签');return
        lib=self.owner.lib;self.saving=True;self.table.setEnabled(False);self.create_button.setEnabled(False)
        def completed(value):
            from .library import load_projection
            self.saving=False;self.table.setEnabled(True);self.name_input.clear()
            def loaded(data):
                projection,tags,groups=data;self.owner.library_page.set_data(projection)
                self.owner.source_page.set_data(projection[3],projection[1]);self.loaded((tags,groups))
            self.owner.run_job('刷新标签与书架',lambda cancel:(load_projection(lib,cancel),M.list_tags(lib),M.list_tag_groups(lib)),loaded,self.failed,cancellable=False)
        try:self.owner.run_job(label,lambda cancel:fn(lib),completed,self.failed,cancellable=False,writes_library=True)
        except Exception as error:self.failed(error)

    def failed(self,error):
        self.saving=False;self.table.setEnabled(True);self.create_button.setEnabled(not self.owner.lib.readonly);self.message.setText(str(error))
