from esdm2python.names import camel, singular, snake, studly, upper_const


def test_studly():
    assert studly("task") == "Task"
    assert studly("add-task") == "AddTask"
    assert studly("completion-changed") == "CompletionChanged"
    assert studly("deleted-tasks") == "DeletedTasks"


def test_camel():
    assert camel("valid-until") == "validUntil"
    assert camel("task") == "task"


def test_snake():
    assert snake("task-added") == "task_added"
    assert snake("CompletionChanged") == "completion_changed"
    assert snake("set-completion") == "set_completion"


def test_singular():
    assert singular("tasks") == "task"
    assert singular(studly("deleted-tasks")) == "DeletedTask"
    assert singular("entries") == "entry"


def test_upper_const():
    assert upper_const("open") == "OPEN"
    assert upper_const("in-progress") == "IN_PROGRESS"
