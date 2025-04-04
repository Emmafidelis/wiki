# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt


import frappe
from frappe.search import web_search
from frappe.utils import strip_html_tags, update_progress_bar
from frappe.utils.redis_wrapper import RedisWrapper

PREFIX = "wiki_page_search_doc"


_redisearch_available = False
try:
	from redis.commands.search.query import Query

	_redisearch_available = True
except ImportError:
	pass


@frappe.whitelist(allow_guest=True)
def search(query, path, space):
	from wiki.wiki_search import WikiSearch

	if not space:
		space = get_space_route(path)

	use_redisearch = frappe.db.get_single_value("Wiki Settings", "use_redisearch_for_search")
	if not use_redisearch or not _redisearch_available:
		result = web_search(query, space)

		for d in result:
			d.title = d.title_highlights or d.title
			d.route = d.path
			d.content = d.content_highlights

			del d.title_highlights
			del d.content_highlights
			del d.path

		return {"docs": result, "search_engine": "frappe_web_search"}

	search = WikiSearch()
	search_query = search.clean_query(query)
	query_parts = search_query.split(" ")

	if len(query_parts) == 1 and not query_parts[0].endswith("*"):
		search_query = f"{query_parts[0]}*"
	if len(query_parts) > 1:
		search_query = " ".join([f"%%{q}%%" for q in query_parts])

	result = search.search(
		f"@title|content:({search_query})",
		space=space,
		start=0,
		sort_by="modified desc",
		highlight=True,
		with_payloads=True,
	)

	docs = []
	for doc in result.docs:
		docs.append(
			{
				"content": doc.content,
				"name": doc.id.split(":", 1)[1],
				"route": doc.route,
				"title": doc.title,
			}
		)

	return {"docs": docs, "search_engine": "redisearch"}


def get_space_route(path):
	for space in frappe.db.get_all("Wiki Space", pluck="route"):
		if space in path:
			return space


def rebuild_index():
	from redis.commands.search.field import TextField
	from redis.commands.search.indexDefinition import IndexDefinition
	from redis.exceptions import ResponseError

	r = frappe.cache()
	r.set_value("wiki_page_index_in_progress", True)

	# Options for index creation
	schema = (
		TextField("title", weight=3.0),
		TextField("content"),
	)

	# Create an index and pass in the schema
	spaces = frappe.db.get_all("Wiki Space", pluck="route")
	wiki_pages = frappe.db.get_all("Wiki Page", fields=["name", "title", "content", "route"])
	for space in spaces:
		try:
			drop_index(space)

			index_def = IndexDefinition(
				prefix=[f"{r.make_key(f'{PREFIX}{space}').decode()}:"], score=0.5, score_field="doc_score"
			)
			r.ft(space).create_index(schema, definition=index_def)

			records_to_index = [
				d
				for d in wiki_pages
				if (space + "/" == d.get("route"))
				or (
					d.get("route").startswith(space + "/")
					and not d.get("route").replace(space + "/", "").startswith("v")
				)
			]
			create_index_for_records(records_to_index, space)
		except ResponseError as e:
			print(e)

	r.set_value("wiki_page_index_in_progress", False)


def rebuild_index_in_background():
	if not frappe.cache().get_value("wiki_page_index_in_progress"):
		print(f"Queued rebuilding of search index for {frappe.local.site}")
		frappe.enqueue(rebuild_index, queue="long")


def create_index_for_records(records, space):
	r = frappe.cache()
	for i, d in enumerate(records):
		if not hasattr(frappe.local, "request") and len(records) > 10:
			update_progress_bar(f"Indexing Wiki Pages - {space}", i, len(records), absolute=True)

		key = r.make_key(f"{PREFIX}{space}:{d.name}").decode()
		mapping = {
			"title": d.title,
			"content": strip_html_tags(d.content),
			"route": d.route,
		}
		super(RedisWrapper, r).hset(key, mapping=mapping)


def remove_index_for_records(records, space):
	from redis.exceptions import ResponseError

	r = frappe.cache()
	for d in records:
		try:
			key = r.make_key(f"{PREFIX}{space}:{d.name}").decode()
			r.ft(space).delete_document(key)
		except ResponseError:
			pass


def update_index(doc):
	record = frappe._dict({"name": doc.name, "title": doc.title, "content": doc.content, "route": doc.route})
	space = get_space_route(doc.route)

	create_index_for_records([record], space)


def remove_index(doc):
	record = frappe._dict(
		{
			"name": doc.name,
			"route": doc.route,
		}
	)
	space = get_space_route(doc.route)

	remove_index_for_records([record], space)


def drop_index(space):
	from redis.exceptions import ResponseError

	try:
		frappe.cache().ft(space).dropindex(delete_documents=True)
	except ResponseError:
		pass
