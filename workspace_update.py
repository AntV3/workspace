 from asyncio import LimitOverrunError
from logging import exception, lastResort
from sys import last_traceback
from threading import ThreadError
from tkinter import mainloop
from typing import overload
import pgdb
import json
from elasticsearch import Elasticsearch

# Update to the corresponding DB credentials
hostname = 'localhost'
username = 'newuser'
password = 'password'
martydb = 'postgres'
alertsdb = 'postgres'

# Connect to the database, create a table to store the correct and duplicate IDs,
# store those IDs in their corresponding columns, and delete the duplicates:
def idCollect( conn ) :

    """
    This function creates a table containing all of the duplicate IDs that we
    are going to delete from the workspaces table. One column contains the ID
    that we are removing, the other column contains the ID that we are keeping
    to be the source of truth for that set of duplicates.
    conn1: This is the connection to the marty database
    conn2: This is the connection to the civicfeed_alerts database
    """

    try:
        cur = conn.cursor()

        # Initially, drop the table if it exists (it does not exist, but this
        # is in case we run the script again) then, create the table to store
        # IDs in the marty DB
        cur.execute('DROP TABLE IF EXISTS id_corrector')
        cur.execute('CREATE table id_corrector(true_id int, false_id int)')
        print('created the id_corrector table in the marty db', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Insert the IDs we are keeping and their duplicates into the table
        cur.execute("insert into id_corrector select t1.id_true, workspaces.id from workspaces join (select min(id) as id_true, title, meta, tags, COALESCE(geo_primary, '') as geo_prim, COALESCE(geo_secondary, '{''}') as geo_sec, count(*) from workspaces group by title, meta, tags, geo_primary, geo_secondary order by count(*) desc) as t1 on workspaces.title = t1.title and workspaces.meta = t1.meta and workspaces.tags = t1.tags and COALESCE(workspaces.geo_primary, '') = t1.geo_prim and COALESCE(workspaces.geo_secondary, '{''}') = t1.geo_sec and t1.id_true <> workspaces.id")
        print('inserted the data into the id_corrector in the marty db', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        cur.execute("select * from workspaces join id_corrector on workspaces.id = id_corrector.false_id")
        duplicateWorkspaces = cur.fetchall()

        for workspace in duplicateWorkspaces:
            print(workspace, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        conn.commit()
        print('committed the changes to marty db', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

    except Exception as err:
        print("idCollect error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

def elasticsearchUpdate ( conn ) :

    """
    This function gets the data from the id_corrector table and converts it to
    both a list and a dictionary. Also, we update the workspace data in ES.
    And finally, we update the ES documents.
    conn: This is the connection to the marty database -- can be to the alerts
    database as well as they both will have the necessary table.
    """

    try:
        cur = conn.cursor()

        # Select both rows from the id_corrector table, then create a list of
        # all the true and false ID's
        cur.execute("select * from id_corrector")
        idListQuery = cur.fetchall()
        print('selected the id_data from the marty db and created a list for the elasticsearch update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Initialize a list and a dicitionary for future use
        idList = []
        idDict = {}

        # Make a list of lists that contain both IDs and
        # create a Dict relating the false_id to the true_id
        for idItem in idListQuery:
            idList.append([idItem.true_id, idItem.false_id])
            idDict[idItem.false_id] = idItem.true_id
        print('Created a python dictionary of the false IDs pointing to the true IDs for the elasticsearch update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Connect to elasticsearch via the credentials Nick provided -- May need to update.
        es = Elasticsearch("https://elastic:348y99tge8ZbzM4zL0km9uR6@es.cluster-ca-dev.peakm.com/")
        print('connected to elasticsearch', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Create empty lists to fill later
        results = []
        docs = []

        print('begin looping through the ES documents', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
        # Loop through the every id we are removing --- This takes a long time
        for i in range(len(idList)):

            # Search for documents that have the IDs we're removing
            res = es.search(index="doc_*", size = 1000, _source = "workspace", body={"query": {"match": {"workspace": idList[i][1]}}})

            # Turn the individual results into a list of documents
            res = res['hits']['hits']
            results.append(res)

            # Print testing to ensure the lists and results are correct
            # also helps us track the progress of the script running
            if results[i] == []:
                print(i, ': empty', file = open('workspaceUpdateOutput.txt', 'a'))
            else:
                print(i, ": ", "id being removed is: ", idList[i][1], '\n', file = open('workspaceUpdateOutput.txt', 'a'))
                print("here are the docs: ", results[i], '\n', file = open('workspaceUpdateOutput.txt', 'a'))
                docs.append(results[i])

        # Print each document, as well as the data that we want from it -- mostly
        #  to track the progress of the script
        for doc in docs:
            for d in doc:
                print(d, '\n', '', file = open('workspaceUpdateOutput.txt', 'a'))

                _index = d['_index']
                print('_index:', _index, file = open('workspaceUpdateOutput.txt', 'a'))

                _id = d['_id']
                print('_id:', _id, file = open('workspaceUpdateOutput.txt', 'a'))

                _body = d['_source']
                print('_body:', _body, file = open('workspaceUpdateOutput.txt', 'a'))

                _body['workspace'] = idDict[_body['workspace']]
                print('_body[workspace]:', _body['workspace'], file = open('workspaceUpdateOutput.txt', 'a'))
                print('_body:', _body, file = open('workspaceUpdateOutput.txt', 'a'))
                print(' ----------- ', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

                # Update each document with the new ID using the dictionary we made earlier
                es.update(index = _index, id = _id, body = {'doc' : _body })

    except Exception as err:
        print('elasticsearch error: ', err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

    print('finished running the ES update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

def cividfeedAlertsUpdate ( martyConn , alertsConn ) :

    """
    This function gets the data from the id_corrector table and converts it to
    both a list and a dictionary. Also, we retrieve data from the alerts table.
    Then, we loop through that data and update the workspace IDs were necessary.
    Finally, we reformat the data into JSON and update the alerts table with
    out new data.
    conn: This is the connection to the alerts database.
    """

    try:
        print('establish the cursors', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        martyCur = martyConn.cursor()
        alertsCur = alertsConn.cursor()

        print('start converting the id_corrector to id data', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Retrieve all the true and false ID's and create a list of them
        martyCur.execute("select * from id_corrector")
        idListQuery = martyCur.fetchall()

        print('selected the id data from the marty db and created a list for the civicfeed_alerts update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Create the ID List and Dictionary to update the IDs
        idList = []
        idDict = {}
        happensListExclude = []
        happensListSource = []

        # Make a list of IDs and a Dict relating the false_id to the true_id
        for idItem in idListQuery:
            idList.append([idItem.true_id, idItem.false_id])
            idDict[idItem.false_id] = idItem.true_id

        print('Created a python dictionary of the false IDs pointing to the true IDs for the civicfeed_alerts update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        #########################################################################
        ######## This section is for the sources key in the alerts talbe ########
        #########################################################################

        # Pull out the happens column from alerts table to alter the sources key
        alertsCur.execute("select id, happens from alerts where happens -> 'value' -> 'sources' is not null and happens -> 'value' -> 'sources' <> '[]'")
        happensListSourceQuery = alertsCur.fetchall()

        print('pulled the happens column from the alerts table', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Loop through our SQL we pulled to create a list that we will use to
        # update the data, and then the db
        for sourceItem in happensListSourceQuery:
            happensListSource.append([sourceItem[0], sourceItem[1]])

        print('made a nested list with item[0] containing the alerts id and item[1] containing the alerts happens column for the happens.value.sources.workspace update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('begin updating the happens.value.sources.workspace data', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
        try:
            # Retrieve the heavily nested data from happensListSource
            # Go through the happensListSource list and go through the nested JSON
            # to get to the 'workspace' JSON key. Then, if that value is one of
            # the ones we're removing, update it to what we're keeping.

            # Shape of happensListSource JSON:

            # {
            #   "text": "appears in Blogs",
            #   "value": {
            #     "tags": [
            #       {
            #        "id": "blogsdiscussions",
            #         "tag": "blogsdiscussions",
            #         "meta": {
            #           "external_agent": "blogsdiscussions"
            #         },
            #         "friendly": "Blogs"
            #       }
            #     ],
            #     "sources": [
            #       {
            #         "_id": "3019",
            #         "slug": "Entrepreneur.com  - entrepreneur.com|entrepreneur.com",
            #         "tags": "[\"news\", \"news-us\", \"news-glob\"]",
            #         "title": "Entrepreneur.com  - entrepreneur.com",
            #         "domain": "entrepreneur.com",
            #         "workspace": "3019"
            #       },
            #       {
            #         "_id": "1852",
            #         "slug": "Forbes  - forbes.com|forbes.com",
            #         "tags": "[\"news\", \"news-us\", \"news-glob\"]",
            #         "title": "Forbes  - forbes.com",
            #         "domain": "forbes.com",
            #         "workspace": "1852"
            #       },
            #       {
            #         "_id": "2544",
            #         "slug": "Inc.  - inc.com|inc.com",
            #         "tags": "[\"news\", \"news-us\", \"news-glob\"]",
            #         "title": "Inc.  - inc.com",
            #         "domain": "inc.com",
            #         "workspace": "2544"
            #       },
            #       {
            #         "_id": "15805",
            #         "slug": "HuffPost  - huffpost.com|huffpost.com",
            #         "tags": "[\"news\", \"addgn\", \"news-us\", \"news-glob\"]",
            #         "title": "HuffPost  - huffpost.com",
            #         "domain": "huffpost.com",
            #         "workspace": "15805"
            #       }
            #     ]
            #   },
            #   "search": "workspaces"
            # }

            for items in happensListSource:
                for item in items[1]['value']['sources']:
                    for id in idList:
                        for it in item:
                            if it['workspace'] == str(id[1]):
                                it['workspace'] = str(idDict[id[1]])
                                it['_id'] = str(idDict[id[1]])

        except Exception as err:
            print("happens.value.sources.workspace error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
            # Pass to the next value if this fails because some rows do not
            # contain all of the JSON keys we access
            pass

        print('finished updating happens.value.sources.workspace', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('begin updating the alerts table with the new data', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        try:
            # Loop through each item in the list to update the table
            for item in happensListSource:
                # Convert the item from type dict to json string
                happens = json.dumps(item[1])
                # Replace single ' with '', as this is the postgresql
                # format to avoid a syntax error for an apostrophe in a string.
                # (Two apostrophes).
                happens = happens.replace("'", "''")
                # Give the item[0] a descriptive variable name, for clarities sake
                id = item[0]
                alertsCur.execute(f"UPDATE alerts set happens = '{happens}' where id = '{id}'")

        except Exception as err:
            print("Alerts update error : ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('finish updating the alerts table with the new data', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        #########################################################################
        ######## This section is for the exclude key in the alerts talbe ########
        #########################################################################

        # Pull out the happens column from alerts table to alter the exclude key
        alertsCur.execute("select id, happens from alerts where happens -> 'value' -> 'exclude' is not null and happens -> 'value' -> 'exclude' <> '[]'")
        happensListExcludeQuery = alertsCur.fetchall()

        print('pull the happens column for the happens.value.exclude update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Loop through our SQL we pulled to create a list that we will use to
        # update the data, and then the db
        for excludeItem in happensListExcludeQuery:
            happensListExclude.append([excludeItem[0], excludeItem[1]])

        print('begin the happens.value.exclude alteration', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        try:
            # Retrieve the heavily nested data from happensListExclude
            # Go through the happensListExclude list and go through the nested JSON
            # to get to the 'workspace' JSON key. Then, if that value is one of
            # the ones we're removing, update it to what we're keeping.

            # Shape of happensListExclude JSON:

            # {
            #   "text": "appears in News, Blogs, TV and Radio",
            #   "value": {
            #     "tags": [
            #       {
            #         "id": 1,
            #         "tag": "news",
            #         "meta": null,
            #         "friendly": "News"
            #       },
            #       {
            #         "id": "blogsdiscussions",
            #         "tag": "blogsdiscussions",
            #         "meta": {
            #           "external_agent": "blogsdiscussions"
            #         },
            #         "friendly": "Blogs"
            #       },
            #       {
            #         "id": "tv-radio",
            #         "tag": "tv",
            #         "meta": {
            #           "external_agent": "tveyes"
            #         },
            #         "friendly": "TV and Radio"
            #       }
            #     ],
            #     "exclude": [
            #       {
            #         "field": "workspace",
            #         "title": "williamsonhomepage.com",
            #         "value": 149227
            #       },
            #       {
            #         "field": "id",
            #         "title": "COVID 2-24-1",
            #         "value": 455232330
            #       },
            #       {
            #         "field": "workspace",
            #         "title": "Yahoo Style",
            #         "value": 31240
            #       },
            #       {
            #         "field": "id",
            #         "title": "Jonathan Van Ness shares COVID-19 vaccine selfie after his HIV status makes him eligible",
            #         "value": 452331687
            #       },
            #       {
            #         "field": "workspace",
            #         "title": "Yahoo News",
            #         "value": 13565
            #       },
            #       {
            #         "field": "id",
            #         "title": "Italian ambassador among 3 killed in attack on Congo convoy",
            #         "value": 452331506
            #       },
            #       {
            #         "field": "id",
            #         "title": "Taco Bell breaks into chicken sandwich war with new menu item",
            #         "value": 452331474
            #       }
            #     ],
            #     "sources": []
            #   },
            #   "search": "workspaces"
            # }

            for items in happensListExclude:
                for item in items[1]['value']['exclude']:
                    if item['field'] == "workspace":
                        for id in idList:
                            if item['value'] == id[1]:
                                item['value'] = idDict[id[1]]

        except Exception as err:
            print("happens.value.exclude.value error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
            # Pass to the next value if this fails because some rows do not
            # contain all of the JSON keys we access
            pass

        print('finished the happens.value.exclude alteration', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('begin the happens.value.exclude table update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Update the table
        try:
            # Loop through each item in the list to update the table
            for item in happensListExclude:
                # Convert the item from type dict to json string
                happens = json.dumps(item[1])
                # Replace single ' with '', as this is the postgresql
                # format to avoid a syntax error for an apostrophe in a string.
                # (Two apostrophes).
                happens = happens.replace("'", "''")
                # Give the item[0] a descriptive variable name, for clarities sake
                id = item[0]
                alertsCur.execute(f"UPDATE alerts set happens = '{happens}' where id = '{id}'")

        except Exception as err:
            print("Alerts update error : ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('finish updating the alerts table with the new data', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Commit our changes to the civicfeed_alerts db
        alertsConn.commit()
        print('changes committed', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

    except Exception as err:
        print('civicfeed_alerts error: ', err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

def docsUpdate( conn ) :

    """
    This function uses a sql query to update the IDs in the docs table we are
    removing to the ones we are keeping by using a relatively straightforward
    join.
    conn: This is the connection to the marty database.
    """

    try:
        cur = conn.cursor()

        print('begin docs update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        idList = []

        cur.execute("select false_id from id_corrector")
        falseID = cur.fetchall()

        # Update the docs table with the unique IDs using the join
        for items in falseID:
            for item in items:
                print('update docs in for loop', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
                cur.execute(f"update docs set workspace = t1.true_id from (select true_id, false_id from id_corrector where false_id = {item}) as t1 where docs.workspace = t1.false_id")
                rows = cur.rowcount
                print(rows, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('finish docs update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Commit the changes to the marty table
        conn.commit()

        print('commit the changes to the docs table', file = open('workspaceUpdateOutput.txt', 'a'))

    except Exception as err:
        print("docsUpdate error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

def agentsUpdate( conn ) :

    """
    This function uses a sql query to update the IDs in the agents table we are
    removing to the ones we are keeping by using a relatively straightforward
    join.
    conn: This is the connection to the marty database.
    """

    try:
        cur = conn.cursor()

        print('begin agents update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        cur.execute("select false_id from id_corrector")
        falseID = cur.fetchall()

        # Update the agents table with the unique IDs using the join
        for items in falseID:
            for item in items:
                print('update agents in a for loop', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
                cur.execute(f"update agents set workspace = t1.true_id from (select true_id, false_id from id_corrector where false_id = {item}) as t1 where agents.workspace = t1.false_id")
                rows = cur.rowcount
                print(rows, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('finish agents update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Commit the changes
        conn.commit()

        print('commit the changes to the agents table', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    except Exception as err:
        print("agentsUpdate error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

def authorsUpdate( conn ) :

    """
    This function uses a sql query to update the IDs in the authors table we are
    removing to the ones we are keeping by using a relatively straightforward
    join.
    conn: This is the connection to the marty database.
    """

    try:
        cur = conn.cursor()

        print('begin authors update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        cur.execute("select true_id, false_id from id_corrector")
        idList = cur.fetchall()

        IDArray = []
        for items in idList:
            IDArray.append([{items[0]}, {items[1]}])

        # Update the agents table with the unique IDs using the join
        for items in idList:
            print('update authors primary_source in a for loop', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
            cur.execute(f"update authors set primary_source = t1.true_id from (select true_id, false_id from id_corrector where false_id = {items[1]}) as t1 where authors.primary_source = t1.false_id")
            rows = cur.rowcount
            print(rows, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        for items in IDArray:
            print('update authors alternative_sources in a for loop', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
            cur.execute(f"update authors set alternative_sources = {items[0]} where cast(authors.alternative_sources as text) = cast({items[1]} as text)")
            rows = cur.rowcount
            print(rows, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        print('finish authors update', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Commit the changes
        conn.commit()

        print('commit the changes to the authors table', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    except Exception as err:
        print("authorsUpdate error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))


def workspacesDelete( conn ) :
    """
    Now that we have updated all of the relevant information, we now delete all
    of the duplicate workspaces. This takes quite a long time. The logic is
    listed below in a block of comments.
    conn: This is the connection to the marty database.
    """

    try:
        cur = conn.cursor()

        print('begin workspace delete', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Delete the duplicate rows
        # Select all the rows that have the same title, meta, tags, geo_primary, and geo_secondary,
        # Then, delete all the rows that have an ID that is not the minimum ID
        # for that data.

        rows = 1
        while True:
            print('delete workspaces in groups', '\n', file = open('workspaceUpdateOutput.txt', 'a'))
            cur.execute("delete from workspaces where id in
            (select id from workspaces join id_corrector on id_corrector.false_id = workspaces.id limit 100)")
            rows = cur.rowcount
            print(rows, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
            if rows == 0:
                break

        print('finish workspace delete', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        cur.execute("select * from workspaces join id_corrector on workspaces.id = id_corrector.false_id")
        duplicateWorkspaces = cur.fetchall()

        for workspace in duplicateWorkspaces:
            print(workspace, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        # Commit the changes
        conn.commit()

        print('commit the changes to workspaces table', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

    except Exception as err:
        print("workspacesDelete error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

try:

    # Connect to the database using the credentials above
    martyConnect = pgdb.connect( host=hostname, user=username, password=password, database=martydb )
    print('connected to marty db', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

    # Change the database name to connect to the civicfeed_alerts db
    alertsConnect = pgdb.connect( host=hostname, user=username, password=password, database=alertsdb )
    print('connected to alerts db', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

except Exception as err:
    print("Connection error: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

try:
    # Run the script with the connection

    print(idCollect.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    idCollect( martyConnect )

    print(elasticsearchUpdate.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    elasticsearchUpdate ( martyConnect )

    print(cividfeedAlertsUpdate.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    cividfeedAlertsUpdate ( martyConnect , alertsConnect )

    print(docsUpdate.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    docsUpdate( martyConnect )

    print(agentsUpdate.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    agentsUpdate( martyConnect )

    print(authorsUpdate.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    authorsUpdate( martyConnect )

    print(workspacesDelete.__doc__, '\n', file = open('workspaceUpdateOutput.txt', 'a'))
    workspacesDelete( martyConnect )

except Exception as err:
    print("Error running scripts: ", err, '\n', file = open('workspaceUpdateOutput.txt', 'a'))

# Close the connection
martyConnect.close()
print('closed marty connection', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

alertsConnect.close()
print('closed alerts connection', '\n', file = open('workspaceUpdateOutput.txt', 'a'))

except as exception
ThreadError
class  ();
def laser():
    pass
laser = mustpass_laser(x=12,y=24,padx=10,pady=25) if LimitOverrunError run;
loop();
if  laser overload:
    run last_traceback
elif():
    quit
mainloop();


except exception as err:
    print('elasticsearch error:'), err, '\n', file= open('workspaceUpdateOutput.txt', 'a'))
print('finished running the ES update', 'n\'', file =open('workspaceUpdateOutput.txt', 'a'))

def cividfeedAlertsUpdate (martyConn , alertsConn ):
    try:
        print('establish the cursors', '\n' file = open('workspaceUpdateOutput.txt', 'a'))
        
        martyCur = martyConn.cursor()
        alertsCur = alertsConn.cursor()

        print(' start converting the id_corrector to id data',  '\n', file = open('workspaceUpdateOutput.txt', 'a'))

        martyCur= martyConn.cursor()
        alertsCur = alertsConn.cursor()

        print()