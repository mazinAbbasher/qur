from django import template

register = template.Library()

@register.inclusion_tag('sidebar.html', takes_context=True)
def sidebar(context):
	# Inclusion tags render with the dict returned here — context processors
	# (which set is_manager / is_salesperson via panel.permissions.role_context)
	# are NOT applied to that dict, so we must forward the role flags explicitly.
	# Without this the {% if is_manager %} blocks in sidebar.html evaluate false
	# and the manager-only links (reports, expenses, finance) vanish for managers.
	return {
		'request': context['request'],
		'active': context.get('active_sidebar', None),
		'is_manager': context.get('is_manager', False),
		'is_salesperson': context.get('is_salesperson', False),
    }