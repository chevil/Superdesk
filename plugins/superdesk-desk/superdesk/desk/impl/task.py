'''
Created on April 2, 2013

@package: superdesk desk
@copyright: 2013 Sourcefabric o.p.s.
@license: http://www.gnu.org/licenses/gpl-3.0.txt
@author: Martin Saturka

Contains the SQL alchemy implementation for desk task API.
'''

from ..api.task import ITaskService, Task
from ..meta.task import TaskMapped, TaskNestMapped
from ..meta.task_status import TaskStatusMapped
from ..meta.task_link import TaskLinkMapped
from ally.container.ioc import injected
from ally.container.support import setup
from ally.exception import InputError, Ref
from ally.internationalization import _
from ally.support.api.util_service import copy
from ally.support.sqlalchemy.util_service import buildQuery, buildLimits, handle
from sql_alchemy.impl.entity import EntityServiceAlchemy
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql.expression import and_, or_
from ally.api.extension import IterPart

# --------------------------------------------------------------------

@injected
@setup(ITaskService, name='taskService')
class TaskServiceAlchemy(EntityServiceAlchemy, ITaskService):
    '''
    Implementation for @see: ITaskService
    '''

    def __init__(self):
        '''
        Construct the desk task service.
        '''
        EntityServiceAlchemy.__init__(self, TaskMapped)

    def getAll(self, deskId=None, userId=None, statusKey=None, offset=None, limit=None, detailed=False, q=None):
        '''
        @see: ITaskService.getAll
        dealing with status and root_only parts
        '''
        sql = self.session().query(TaskMapped)
        if deskId:
            sql = sql.filter(TaskMapped.Desk == deskId)
        if userId:
            sql = sql.filter(TaskMapped.User == userId)
        sql = sql.filter(TaskMapped.Parent == None)
        if statusKey:
            sql = sql.join(TaskStatusMapped).filter(TaskStatusMapped.Key == statusKey)
        if q:
            sql = buildQuery(sql, q, TaskMapped)
        sqlLimit = buildLimits(sql, offset, limit)
        if detailed: return IterPart(sqlLimit.all(), sql.count(), offset, limit)
        return sqlLimit.all()

    def insert(self, task):
        '''
        @see: ITaskService.insert
        dealing with status, parent and nested_sets parts
        '''
        assert isinstance(task, Task), 'Invalid task %s' % task
        taskDb = TaskMapped()
        taskDb.statusId = self._statusId(task.Status)
        copy(task, taskDb, exclude=('Status', 'Parent',))

        # putting into nested sets
        nestDb = TaskNestMapped()
        nestDb.upperBar = 1
        nestDb.lowerBar = 2

        taskDb.Parent = None
        if task.Parent is not None:
            parentDb = self.session().query(TaskMapped).get(task.Parent)
            if not parentDb: raise InputError(Ref(_('Unknown parent task id'), ref=Task.Parent))
            taskDb.Parent = parentDb.Id
            nestDbParent = self.session().query(TaskNestMapped).get(parentDb.Id)
            if not nestDbParent: raise InputError(Ref(_('Unknown parent task %(task)d') % dict(task=parentDb.Id),))
        else:
            nestDbParent = None

        try:
            self.session().add(taskDb)
            self.session().flush((taskDb,))

            nestDb.task = taskDb.Id
            nestDb.group = taskDb.Id

            self.session().add(nestDb)
            self.session().flush((nestDb,))

        except SQLAlchemyError as e:
            handle(e, taskDb)

        if nestDbParent: self._attach(nestDbParent, nestDb)

        task.Id = taskDb.Id
        return task.Id

    def update(self, task):
        '''
        @see: ITaskService.update
        dealing with status and parent parts
        '''
        assert isinstance(task, Task), 'Invalid task %s' % task
        taskDb = self.session().query(TaskMapped).get(task.Id)
        if not taskDb: raise InputError(Ref(_('Unknown task id'), ref=Task.Id))
        if Task.Status in task: taskDb.statusId = self._statusId(task.Status)

        if Task.Parent in task:
            if taskDb.Parent != task.Parent:
                nestDb = self.session().query(TaskNestMapped).get(taskDb.Id)
                if not nestDb: raise InputError(Ref(_('Unknown task'), ref=Task.Id))
                
                if task.Parent is None:
                    self._detach(nestDb)
                    taskDb.Parent = None
                    
                elif taskDb.Parent != task.Parent:
                    nestDbParent = self.session().query(TaskNestMapped).get(task.Parent)
                    if not nestDbParent: raise InputError(Ref(_('Unknown parent task id'), ref=Task.Parent))
                    taskDb.Parent = task.Parent

                    # check whether the subtree_node is not an ancestor of the parent_node
                    if nestDb.group == nestDbParent.group and nestDb.upperBar <= nestDbParent.upperBar and \
                    nestDb.lowerBar >= nestDbParent.lowerBar:
                        raise InputError(Ref(_('Parent task %(parent)d is subtask of %(task)d') % dict(parent=task.Parent, task=taskDb.Id),))
                    
                    self._detach(nestDb)
                    self._attach(nestDbParent, nestDb)

        try:
            self.session().flush((copy(task, taskDb, exclude=('Status', 'Parent')),))
        except SQLAlchemyError as e: handle(e, TaskMapped)


    def delete(self, taskId):
        '''
        @see: ITaskService.delete
        dealing with nested_sets part
        '''

        taskDb = self.session().query(TaskMapped).get(taskId)
        if not taskDb: raise InputError(Ref(_('Unknown task %(task)d') % dict(task=taskId),))
        task_nestDb = None
        try:
            task_nestDb = self.session().query(TaskNestMapped).filter(TaskNestMapped.task == taskId).one()
        except NoResultFound:
            raise InputError(Ref(_('Unknown task %(task)d') % dict(task=taskId),))

        if (task_nestDb.upperBar + 1) != task_nestDb.lowerBar:
            raise InputError(Ref(_('Task %(task)d has subtasks') % dict(task=taskId), ref=Task.Id))

        if taskDb.Parent:
            self._detach(task_nestDb)

        try:
            sql = self.session().query(TaskLinkMapped)
            sql = sql.filter(or_(TaskLinkMapped.Head == taskId, TaskLinkMapped.Tail == taskId))
            sql = sql.delete()
        except:
            pass

        self.session().delete(task_nestDb)
        self.session().delete(taskDb)

        return True

    def getSubtasks(self, taskId, statusKey=None, offset=None, limit=None, detailed=False, q=None):
        '''
        @see: ITaskService.getSubtasks
        '''
        return self._getSubnodes(taskId, False, statusKey, offset, limit, detailed, q)

    def getSubtree(self, taskId, statusKey=None, offset=None, limit=None, detailed=False, q=None):
        '''
        @see: ITaskService.getSubtree
        '''
        return self._getSubnodes(taskId, True, statusKey, offset, limit, detailed, q)

    # ----------------------------------------------------------------

    def _getSubnodes(self, taskId, wholeSubtree, statusKey=None, offset=None, limit=None, detailed=False, q=None):
        '''
        Makes the task list retrieval.
        '''
        taskDb = self.session().query(TaskMapped).get(taskId)
        if not taskDb: raise InputError(Ref(_('Unknown task %(task)d') % dict(task=taskId),))
        task_nestDb = None
        try:
            task_nestDb = self.session().query(TaskNestMapped).filter(TaskNestMapped.task == taskId).one()
        except NoResultFound:
            raise InputError(Ref(_('Unknown task %(task)d') % dict(task=taskId),))

        sql = self.session().query(TaskMapped)

        if not wholeSubtree:
            sql = sql.filter(TaskMapped.Parent == taskId)

        if statusKey:
            sql = sql.join(TaskStatusMapped).filter(TaskStatusMapped.Key == statusKey)
        if q:
            sql = buildQuery(sql, q, TaskMapped)

        sql = sql.join(TaskNestMapped)
        sql = sql.filter(TaskNestMapped.group == task_nestDb.group)
        sql = sql.filter(TaskNestMapped.upperBar > task_nestDb.upperBar)
        sql = sql.filter(TaskNestMapped.lowerBar < task_nestDb.lowerBar)

        sqlLimit = buildLimits(sql, offset, limit)
        if detailed: return IterPart(sqlLimit.all(), sql.count(), offset, limit)
        return sqlLimit.all()

    def _statusId(self, key):
        '''
        Provides the task status id that has the provided key.
        '''
        try:
            sql = self.session().query(TaskStatusMapped.id).filter(TaskStatusMapped.Key == key)
            return sql.one()[0]
        except NoResultFound:
            raise InputError(Ref(_('Invalid task status %(status)s') % dict(status=key), ref=Task.Status))

    def _attach(self, task_nestDb, subtask_nestDb):
        '''
        Sets the nested sets on subtask attaching.
        '''

        # since the subtree_node is a root of its (sub)tree here, and parent is not in that (sub)tree,
        # we are sure that groups of parent_node and subtree_node are different
        t_group = task_nestDb.group
        s_group = subtask_nestDb.group

        s_upper = subtask_nestDb.upperBar
        s_lower = subtask_nestDb.lowerBar
        s_width = 1 + s_lower - s_upper

        t_lower = task_nestDb.lowerBar

        d_lift = t_lower - s_upper

        # forming the gap in base tree, on the lower bars
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == t_group)
        sql = sql.filter(TaskNestMapped.lowerBar >= t_lower)
        sql = sql.update({TaskNestMapped.lowerBar: TaskNestMapped.lowerBar + s_width})

        # forming the gap in base tree, on the upper bars
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == t_group)
        sql = sql.filter(TaskNestMapped.upperBar >= t_lower)
        sql = sql.update({TaskNestMapped.upperBar: TaskNestMapped.upperBar + s_width})

        # moving the new subtree down
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == s_group)
        sql = sql.update({TaskNestMapped.upperBar: (TaskNestMapped.upperBar + d_lift), TaskNestMapped.lowerBar: (TaskNestMapped.lowerBar + d_lift)})

        # setting new group id for the sub-tree
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == s_group)
        sql = sql.update({TaskNestMapped.group: t_group})

        return True

    def _detach(self, subtask_nestDb):
        '''
        Sets the nested sets on subtask detaching.
        '''

        t_group = subtask_nestDb.group
        s_group = subtask_nestDb.id

        if t_group == s_group:
            return True

        s_upper = subtask_nestDb.upperBar
        s_lower = subtask_nestDb.lowerBar
        s_width = 1 + s_lower - s_upper
        s_lift = s_upper - 1

        # setting new group id for the sub-tree
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == t_group)
        sql = sql.filter(and_(TaskNestMapped.upperBar >= s_upper, TaskNestMapped.lowerBar <= s_lower))
        sql = sql.update({TaskNestMapped.group: s_group})

        # removing the gap in remaining tree, on the upper bars
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == t_group)
        sql = sql.filter(TaskNestMapped.upperBar > s_upper)
        sql = sql.update({TaskNestMapped.upperBar: TaskNestMapped.upperBar - s_width})

        # removing the gap in remaining tree, on the lower bars
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == t_group)
        sql = sql.filter(TaskNestMapped.lowerBar > s_upper)
        sql = sql.update({TaskNestMapped.lowerBar: TaskNestMapped.lowerBar - s_width})

        # moving the new tree up
        sql = self.session().query(TaskNestMapped).filter(TaskNestMapped.group == s_group)
        sql = sql.update({TaskNestMapped.upperBar: (TaskNestMapped.upperBar - s_lift), TaskNestMapped.lowerBar: (TaskNestMapped.lowerBar - s_lift)})

        return True

